#!/usr/bin/env python3
"""Typed, sanitized command evidence for hooks and distill-runbook."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from pathlib import Path
from typing import Any, Iterable

from lib_sanitize import residual_credential_kinds, sanitize


ALLOWED_SHELL_TOOLS = frozenset(
    {"Bash", "exec_command", "shell", "unified_exec"}
)
OUTCOMES = frozenset({"success", "error", "unknown"})
SESSION_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
MAX_COMMAND_BYTES = 16_384
MAX_CWD_BYTES = 4_096
MAX_RESULT_BYTES = 4_096
MAX_USER_PAYLOAD_BYTES = 24_576
USER_REQUIRED = frozenset(
    {
        "schema_version",
        "command",
        "cwd",
        "provenance_session",
        "origin",
        "outcome",
    }
)
USER_OPTIONAL = frozenset({"result_excerpt"})


class EvidenceError(ValueError):
    """Command evidence is malformed or unsafe to persist."""


def _bounded_text(value: object, label: str, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or not value
        or "\0" in value
        or len(value.encode("utf-8")) > maximum
    ):
        raise EvidenceError(f"{label} must be a non-empty bounded string")
    return value


def _session(value: object, label: str) -> str:
    if not isinstance(value, str) or not SESSION_RE.fullmatch(value):
        raise EvidenceError(f"{label} must be a bounded identifier")
    return value


def _sanitize_text(value: object, label: str, maximum: int) -> str:
    raw = _bounded_text(value, label, maximum)
    clean, _ = sanitize(raw)
    if residual_credential_kinds(clean):
        raise EvidenceError(f"{label} contains residual credential material")
    return clean


def _task_provenance(root: Path, execution_session: str) -> str:
    origin_file = root / ".task-origin-session"
    try:
        origin = origin_file.read_text(encoding="utf-8").strip()
    except OSError:
        origin = ""
    if SESSION_RE.fullmatch(origin):
        return origin
    meta = root / ".task-meta.json"
    try:
        value = json.loads(meta.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        value = {}
    origin = value.get("origin_session") if isinstance(value, dict) else ""
    return origin if isinstance(origin, str) and SESSION_RE.fullmatch(origin) else execution_session


def _literal_exec_calls(source: str) -> list[dict[str, Any]]:
    """Read literal JSON arguments from canonical tools.exec_command calls."""

    if len(source.encode("utf-8")) > MAX_USER_PAYLOAD_BYTES:
        return []
    decoder = json.JSONDecoder()
    needle = "tools.exec_command("
    offset = 0
    calls: list[dict[str, Any]] = []
    while True:
        start = source.find(needle, offset)
        if start < 0:
            break
        line_start = source.rfind("\n", 0, start) + 1
        prefix = source[line_start:start]
        if not re.fullmatch(
            r"\s*(?:const\s+[A-Za-z_$][A-Za-z0-9_$]*\s*=\s*await\s+|await\s+)",
            prefix,
        ):
            offset = start + len(needle)
            continue
        value_start = start + len(needle)
        while value_start < len(source) and source[value_start].isspace():
            value_start += 1
        try:
            value, consumed = decoder.raw_decode(source[value_start:])
        except json.JSONDecodeError:
            offset = value_start
            continue
        closing = value_start + consumed
        while closing < len(source) and source[closing].isspace():
            closing += 1
        if isinstance(value, dict) and closing < len(source) and source[closing] == ")":
            calls.append(value)
        offset = max(closing + 1, value_start + 1)
    return calls


def _commands(payload: dict[str, Any]) -> list[tuple[str, str]]:
    tool = payload.get("tool_name")
    if tool not in ALLOWED_SHELL_TOOLS:
        return []
    tool_input = payload.get("tool_input")
    fallback_cwd = payload.get("cwd")
    if tool == "Bash":
        calls = [tool_input] if isinstance(tool_input, dict) else []
        command_key = "command"
    elif tool == "exec_command":
        calls = [tool_input] if isinstance(tool_input, dict) else []
        command_key = "cmd"
    elif tool == "shell":
        calls = [tool_input] if isinstance(tool_input, dict) else []
        command_key = "command"
    else:
        source = ""
        if isinstance(tool_input, str):
            source = tool_input
        elif isinstance(tool_input, dict):
            candidates = [
                value
                for key in ("source", "code", "input")
                if isinstance((value := tool_input.get(key)), str) and value
            ]
            if len(candidates) == 1:
                source = candidates[0]
        calls = _literal_exec_calls(source) if source else []
        command_key = "cmd"
    normalized: list[tuple[str, str]] = []
    for call in calls:
        command = call.get(command_key)
        cwd = call.get("workdir", fallback_cwd)
        if not isinstance(command, str) or not isinstance(cwd, str):
            continue
        try:
            clean_command = _sanitize_text(command, "command", MAX_COMMAND_BYTES)
            clean_cwd = _bounded_text(cwd, "cwd", MAX_CWD_BYTES)
        except EvidenceError:
            continue
        if not Path(clean_cwd).is_absolute():
            continue
        normalized.append((clean_command, clean_cwd))
    return normalized


def _outcome(response: object) -> str:
    if not isinstance(response, dict):
        return "unknown"
    if response.get("is_error") or response.get("interrupted"):
        return "error"
    exit_code = response.get("exit_code")
    if type(exit_code) is int:
        return "success" if exit_code == 0 else "error"
    return "success"


def _event_id(parts: dict[str, object]) -> str:
    encoded = json.dumps(
        parts, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _log_path(root: Path) -> Path:
    return root / ".vault-meta" / "command-log.jsonl"


def _read_records(root: Path) -> list[dict[str, Any]]:
    path = _log_path(root)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    records: list[dict[str, Any]] = []
    for line in lines:
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            records.append(value)
    return records


def _append_unique(root: Path, records: Iterable[dict[str, Any]]) -> int:
    candidates = list(records)
    if not candidates:
        return 0
    existing = {
        value
        for record in _read_records(root)
        if isinstance((value := record.get("event_id")), str)
        and SHA256_RE.fullmatch(value)
    }
    accepted = []
    for record in candidates:
        if record["event_id"] in existing:
            continue
        existing.add(record["event_id"])
        accepted.append(record)
    if not accepted:
        return 0
    path = _log_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for record in accepted:
            handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
    return len(accepted)


def capture_agent_payload(root: Path, payload: dict[str, Any]) -> int:
    execution = _session(payload.get("session_id"), "execution_session")
    provenance = _task_provenance(root, execution)
    outcome = _outcome(payload.get("tool_response"))
    call_id = next(
        (
            value
            for key in ("tool_use_id", "tool_call_id", "call_id")
            if isinstance((value := payload.get(key)), str) and value
        ),
        "",
    )
    tool = str(payload.get("tool_name") or "")
    records = []
    for index, (command, cwd) in enumerate(_commands(payload)):
        identity = {
            "origin": "agent-executed",
            "execution_session": execution,
            "provenance_session": provenance,
            "tool_name": tool,
            "call_id": call_id,
            "command_index": index,
            "cwd": cwd,
            "command": command,
            "outcome": outcome,
        }
        records.append(
            {
                "schema_version": 2,
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "session_id": execution,
                "execution_session": execution,
                "provenance_session": provenance,
                "origin": "agent-executed",
                "cwd": cwd,
                "command": command,
                "outcome": outcome,
                "is_error": outcome == "error",
                "event_id": _event_id(identity),
            }
        )
    return _append_unique(root, records)


def ingest_user(root: Path, payload: object) -> str:
    if not isinstance(payload, dict) or set(payload) - USER_REQUIRED - USER_OPTIONAL or not USER_REQUIRED.issubset(payload):
        raise EvidenceError("user evidence keys are invalid")
    if payload.get("schema_version") != 1 or payload.get("origin") != "user-reported":
        raise EvidenceError("user evidence contract is invalid")
    command = _sanitize_text(payload.get("command"), "command", MAX_COMMAND_BYTES)
    cwd = _bounded_text(payload.get("cwd"), "cwd", MAX_CWD_BYTES)
    if not Path(cwd).is_absolute():
        raise EvidenceError("cwd must be absolute")
    provenance = _session(payload.get("provenance_session"), "provenance_session")
    outcome = payload.get("outcome")
    if outcome not in OUTCOMES:
        raise EvidenceError("outcome must be success, error, or unknown")
    result_excerpt = None
    if "result_excerpt" in payload:
        result_excerpt = _sanitize_text(
            payload.get("result_excerpt"), "result_excerpt", MAX_RESULT_BYTES
        )
    identity: dict[str, object] = {
        "origin": "user-reported",
        "provenance_session": provenance,
        "cwd": cwd,
        "command": command,
        "outcome": outcome,
    }
    if result_excerpt is not None:
        identity["result_excerpt"] = result_excerpt
    record: dict[str, Any] = {
        "schema_version": 2,
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "session_id": provenance,
        "execution_session": "",
        "provenance_session": provenance,
        "origin": "user-reported",
        "cwd": cwd,
        "command": command,
        "outcome": outcome,
        "is_error": outcome == "error",
        "event_id": _event_id(identity),
    }
    if result_excerpt is not None:
        record["result_excerpt"] = result_excerpt
    return "recorded" if _append_unique(root, [record]) else "duplicate"


def _normalized(record: dict[str, Any]) -> dict[str, Any] | None:
    origin = record.get("origin")
    if origin not in {"agent-executed", "user-reported"}:
        origin = "agent-executed"
    legacy_session = record.get("session_id")
    execution = record.get("execution_session", legacy_session if origin == "agent-executed" else "")
    provenance = record.get("provenance_session", legacy_session)
    if not isinstance(provenance, str) or not SESSION_RE.fullmatch(provenance):
        return None
    if origin == "agent-executed" and (
        not isinstance(execution, str) or not SESSION_RE.fullmatch(execution)
    ):
        return None
    value = dict(record)
    value.update(
        {
            "origin": origin,
            "execution_session": execution,
            "provenance_session": provenance,
            "outcome": record.get("outcome")
            if record.get("outcome") in OUTCOMES
            else ("error" if record.get("is_error") else "success"),
        }
    )
    return value


def collect(
    root: Path, provenance_session: str, execution_session: str = ""
) -> list[dict[str, Any]]:
    provenance = _session(provenance_session, "provenance_session")
    execution = (
        _session(execution_session, "execution_session")
        if execution_session
        else ""
    )
    selected = []
    for raw in _read_records(root):
        record = _normalized(raw)
        if record is None or record["provenance_session"] != provenance:
            continue
        if (
            record["origin"] == "agent-executed"
            and execution
            and record["execution_session"] != execution
        ):
            continue
        selected.append(record)
    return selected


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("ingest-user")
    sessions = sub.add_parser("sessions")
    sessions.add_argument("--provenance-session", required=True)
    collection = sub.add_parser("collect")
    collection.add_argument("--provenance-session", required=True)
    collection.add_argument("--execution-session", default="")
    args = parser.parse_args()
    root = args.root.expanduser().resolve()
    try:
        if args.command == "ingest-user":
            raw = sys.stdin.read(MAX_USER_PAYLOAD_BYTES + 1)
            if len(raw.encode("utf-8")) > MAX_USER_PAYLOAD_BYTES:
                raise EvidenceError("user evidence payload is oversized")
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise EvidenceError("user evidence must be one JSON object") from exc
            print(json.dumps({"status": ingest_user(root, payload)}, sort_keys=True))
            return 0
        records = collect(root, args.provenance_session)
        if args.command == "sessions":
            groups: dict[str, int] = {}
            user_reported = 0
            for record in records:
                if record["origin"] == "user-reported":
                    user_reported += 1
                else:
                    session = str(record["execution_session"])
                    groups[session] = groups.get(session, 0) + 1
            print(
                json.dumps(
                    {
                        "provenance_session": args.provenance_session,
                        "execution_sessions": dict(sorted(groups.items())),
                        "user_reported": user_reported,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
            return 0
        selected = collect(
            root, args.provenance_session, args.execution_session
        )
        print(
            json.dumps(
                {
                    "provenance_session": args.provenance_session,
                    "execution_session": args.execution_session,
                    "counts": {
                        "agent_executed": sum(
                            item["origin"] == "agent-executed" for item in selected
                        ),
                        "user_reported": sum(
                            item["origin"] == "user-reported" for item in selected
                        ),
                    },
                    "records": selected,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0
    except (EvidenceError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
