#!/usr/bin/env python3
"""Plan one deterministic, optimistic malformed-reap-log-block repair."""

from __future__ import annotations

import hashlib
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from vault_schema import LOG_ENTRY_RX, rewrite_wikilinks


ROOT = Path(__file__).resolve().parents[1]

BLOCK_HEADING_RX = re.compile(r"(?m)^## \[")


class ReapLogRepairError(ValueError):
    """One bound reap log-block repair request is invalid or unsafe."""


@dataclass(frozen=True)
class ReapLogBinding:
    task_name: str
    expected_log_sha256: str
    replacement_entry: str

    def payload(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "task_name": self.task_name,
            "expected_log_sha256": self.expected_log_sha256,
            "replacement_entry": self.replacement_entry,
        }


@dataclass(frozen=True)
class ReapLogRepairPlan:
    repair_id: str
    payload: dict


def parse_reap_log_binding(value: object) -> ReapLogBinding:
    """Parse the strict binding carried by planner and writer."""

    if not isinstance(value, dict) or set(value) != {
        "schema_version",
        "task_name",
        "expected_log_sha256",
        "replacement_entry",
    }:
        raise ReapLogRepairError("reap log binding has an invalid shape")
    if type(value.get("schema_version")) is not int or value["schema_version"] != 1:
        raise ReapLogRepairError("reap log binding schema_version must be 1")
    task_name = value.get("task_name")
    expected = value.get("expected_log_sha256")
    replacement = value.get("replacement_entry")
    if (
        not isinstance(task_name, str)
        or task_name != task_name.strip()
        or not task_name
        or len(task_name) > 200
        or any(token in task_name for token in ("\0", "\n", "\r", "[", "]", "|"))
    ):
        raise ReapLogRepairError("reap log binding task_name is invalid")
    if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise ReapLogRepairError(
            "reap log binding expected_log_sha256 must be a lowercase SHA-256"
        )
    if (
        not isinstance(replacement, str)
        or not replacement.strip()
        or len(replacement) > 4000
    ):
        raise ReapLogRepairError("reap log binding replacement_entry is invalid")
    return ReapLogBinding(task_name, expected, replacement.strip())


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _is_malformed(text: str) -> bool:
    return rewrite_wikilinks(text, lambda _link: None).malformed


def build_reap_log_repair_plan(
    repo_root: Path, binding: ReapLogBinding
) -> ReapLogRepairPlan:
    """Return one complete vault-write payload or fail closed."""

    log = (repo_root.resolve() / "wiki" / "log.md")
    try:
        log_text = log.read_text(encoding="utf-8")
    except OSError as exc:
        raise ReapLogRepairError(f"cannot read wiki/log.md: {exc}") from exc
    if _sha256(log_text) != binding.expected_log_sha256:
        raise ReapLogRepairError("wiki/log.md drifted from the bound SHA-256")

    starts = [match.start() for match in BLOCK_HEADING_RX.finditer(log_text)]
    if not starts:
        raise ReapLogRepairError("wiki/log.md has no entry blocks")
    bounds = list(zip(starts, starts[1:] + [len(log_text)]))
    heading_rx = re.compile(
        r"## \[\d{4}-\d{2}-\d{2}( \d{2}:\d{2})?\] reap \| "
        + re.escape(binding.task_name)
        + r"$"
    )
    matches = [
        (start, end)
        for start, end in bounds
        if heading_rx.fullmatch(log_text[start:end].splitlines()[0].rstrip())
    ]
    if not matches:
        raise ReapLogRepairError("bound reap block is missing")
    if len(matches) > 1:
        raise ReapLogRepairError("bound reap block is duplicated")
    start, end = matches[0]
    if start != starts[0]:
        raise ReapLogRepairError("bound reap block is not the top log entry")
    block = log_text[start:end]
    if not _is_malformed(block):
        raise ReapLogRepairError("bound reap block is not malformed")
    remainder = log_text[:start] + log_text[end:]
    if _is_malformed(remainder):
        raise ReapLogRepairError(
            "wiki/log.md carries unrelated malformed bytes outside the bound block"
        )

    replacement = binding.replacement_entry
    replacement_lines = replacement.splitlines()
    block_heading = block.splitlines()[0].rstrip()
    if LOG_ENTRY_RX.fullmatch(replacement_lines[0]) is None:
        raise ReapLogRepairError("replacement entry heading is not a log heading")
    if replacement_lines[0] != block_heading:
        raise ReapLogRepairError(
            "replacement entry heading must equal the bound block heading"
        )
    if len(BLOCK_HEADING_RX.findall(replacement)) != 1:
        raise ReapLogRepairError("replacement entry must be exactly one log block")
    if _is_malformed(replacement):
        raise ReapLogRepairError("replacement entry has malformed wikilink syntax")

    tail = "\n\n" if end < len(log_text) else "\n"
    repaired = log_text[:start] + replacement + tail + log_text[end:]
    repair_id = "reap-log-repair-" + _sha256(
        f"{binding.expected_log_sha256}:{_sha256(repaired)}"
    )[:16]
    payload = {
        "schema_version": 1,
        "request_id": repair_id,
        "actor": "reap-log-repair",
        "pages": [
            {
                "op": "update",
                "path": "wiki/log.md",
                "content": repaired,
                "expected_sha256": binding.expected_log_sha256,
            }
        ],
        "reap_log_repair": binding.payload(),
    }
    return ReapLogRepairPlan(repair_id=repair_id, payload=payload)


def main(argv: list[str]) -> int:
    if argv:
        print(
            "vault-reap-log-repair: binding is read from stdin; no flags supported",
            file=sys.stderr,
        )
        return 3
    try:
        binding = parse_reap_log_binding(json.load(sys.stdin))
        plan = build_reap_log_repair_plan(ROOT, binding)
    except (ReapLogRepairError, json.JSONDecodeError) as exc:
        print(f"vault-reap-log-repair: rejected: {exc}", file=sys.stderr)
        return 3
    print(
        json.dumps(
            {
                "schema_version": 1,
                "status": "planned",
                "repair_id": plan.repair_id,
                "payload": plan.payload,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
