#!/usr/bin/env python3
"""Hermetic Claude/Codex transcript discovery and session grouping tests."""

from __future__ import annotations

import datetime
import importlib.util
import json
import os
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "scripts" / "session-map.py"


def check(label: str, condition: bool, detail: str = "") -> None:
    if not condition:
        raise SystemExit(f"FAIL {label}: {detail}")
    print(f"OK   {label}")


def load_module():
    spec = importlib.util.spec_from_file_location("session_map_test", MODULE)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def write_jsonl(path: Path, records: list[dict], mtime: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
    os.utime(path, (mtime, mtime))


module = load_module()
date = "2026-07-10"
mtime = datetime.datetime(2026, 7, 10, 12, 0).timestamp()
claude_id = "45deeb96-5035-4bca-9dee-2033c84ce911"
codex_id = "019f0000-0000-7000-8000-000000000001"
fallback_id = "019f0000-0000-7000-8000-000000000002"

with tempfile.TemporaryDirectory(prefix="session-map-test.") as raw:
    root = Path(raw) / "repo"
    claude_root = Path(raw) / "claude-project"
    codex_root = Path(raw) / "codex-sessions"
    index = root / ".vault-meta" / "index.jsonl"
    index.parent.mkdir(parents=True)

    module.VAULT = root
    module.INDEX = index
    module.CLAUDE_PROJ = claude_root
    module.CODEX_ROOT = codex_root

    write_jsonl(
        claude_root / f"{claude_id}.jsonl",
        [
            {
                "type": "user",
                "message": {"content": "Investigate Claude session grouping for the daily status page"},
            }
        ],
        mtime,
    )
    write_jsonl(
        codex_root / "2026/07/10" / f"rollout-{codex_id}.jsonl",
        [
            {"type": "session_meta", "payload": {"id": codex_id, "cwd": str(root)}},
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "Implement Codex session grouping for daily pages"}],
                },
            },
        ],
        mtime + 60,
    )
    write_jsonl(
        codex_root / "2026/07/10" / f"rollout-{fallback_id}.jsonl",
        [
            {"type": "session_meta", "payload": {"id": fallback_id, "cwd": str(root)}},
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "# AGENTS.md instructions\nIgnore as wrapper"}],
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "Add factual runtime labels without guessing from identifiers"}],
                },
            },
        ],
        mtime + 120,
    )
    write_jsonl(
        codex_root / "2026/07/10" / "rollout-other-repo.jsonl",
        [{"type": "session_meta", "payload": {"id": "019f0000-0000-7000-8000-000000000000", "cwd": str(Path(raw) / "other")}}],
        mtime + 180,
    )
    index.write_text(
        json.dumps({"title": "Claude Page", "type": "concept", "sessions": [claude_id]})
        + "\n"
        + json.dumps({"title": "Codex Page", "type": "concept", "sessions": [codex_id]})
        + "\n",
        encoding="utf-8",
    )

    sessions = module.sessions_on(date)
    check("both runtimes discovered", {(item["runtime"], item["session"]) for item in sessions} == {("claude", claude_id), ("codex", codex_id), ("codex", fallback_id)})
    rows = module.build(date)
    by_id = {row["session"]: row for row in rows}
    check("Claude runtime is factual", by_id[claude_id]["runtime"] == "claude")
    check("Codex runtime is factual", by_id[codex_id]["runtime"] == "codex")
    check("wiki labels preferred", by_id[claude_id]["label"] == "Claude Page" and by_id[codex_id]["label"] == "Codex Page")
    check("Codex wrapper prompt skipped", by_id[fallback_id]["label"].startswith("Add factual runtime labels"))

    rendered = "\n".join(module.render_markdown(rows))
    check("Claude heading rendered", "### Claude\n\n- Claude Page" in rendered)
    check("Codex heading rendered", "### Codex\n\n- Codex Page" in rendered)
    check("runtime groups deterministic", rendered.index("### Claude") < rendered.index("### Codex"))
    check("other repository excluded", "019f0000-0000-7000-8000-000000000000" not in rendered)

print("\nAll session map tests passed.")
