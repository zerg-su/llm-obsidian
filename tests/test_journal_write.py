#!/usr/bin/env python3
"""Hermetic writer-backed journal operation tests."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def check(label: str, condition: bool, detail: str = "") -> None:
    if not condition:
        raise SystemExit(f"FAIL {label}: {detail}")
    print(f"OK   {label}")


def run(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ, LLM_OBSIDIAN_ROOT=str(root), CODEX_THREAD_ID="019f0000-0000-7000-8000-000000000001")
    return subprocess.run([sys.executable, str(root / "scripts/journal-write.py"), *args], cwd=root, env=env, text=True, capture_output=True)


with tempfile.TemporaryDirectory(prefix="journal-write-test.") as raw:
    root = Path(raw)
    (root / "scripts").mkdir()
    (root / "_templates").mkdir()
    (root / ".vault-meta").mkdir()
    for name in (
        "daily_contract.py", "journal-write.py", "vault-write.py",
        "plan_lifecycle.py", "vault_schema.py", "pipeline_events.py",
    ):
        shutil.copy2(ROOT / "scripts" / name, root / "scripts" / name)
    shutil.copy2(ROOT / "_templates/daily.md", root / "_templates/daily.md")
    helper = root / "scripts/current-session-id.sh"
    helper.write_text("#!/usr/bin/env bash\necho \"${CODEX_THREAD_ID:-unknown}\"\n", encoding="utf-8")
    helper.chmod(0o755)
    session_map = root / "scripts/session-map.py"
    session_map.write_text(
        "print('### Codex\\n\\n- Wiki work · `019f0000-0000-7000-8000-000000000001`')\n",
        encoding="utf-8",
    )

    result = run(root, "ensure", "--date", "2026-07-10")
    check("ensure creates through writer", result.returncode == 0, result.stderr)
    page = root / "wiki/daily/2026/07/2026-07-10.md"
    check("canonical page exists", page.is_file() and "## Заметки" in page.read_text(encoding="utf-8"))
    result = run(root, "append", "--date", "2026-07-10", "--section", "plans", "--text", "Ship daily fast path")
    check("plan append", result.returncode == 0, result.stderr)
    result = run(root, "append", "--date", "2026-07-10", "--section", "plans", "--text", "Ship daily fast path")
    check("duplicate append tolerated", result.returncode == 0, result.stderr)
    check("plan deduplicated", page.read_text(encoding="utf-8").count("Ship daily fast path") == 1)
    result = run(root, "check", "--date", "2026-07-10", "--match", "daily fast")
    check("plan check", result.returncode == 0 and "- [x] Ship daily" in page.read_text(encoding="utf-8"), result.stderr)
    result = run(root, "append", "--date", "2026-07-10", "--section", "plans", "--text", "Carry me")
    check("second plan append", result.returncode == 0, result.stderr)
    result = run(root, "carryover", "--source", "2026-07-10", "--target", "2026-07-11")
    target = root / "wiki/daily/2026/07/2026-07-11.md"
    check("carryover target created", result.returncode == 0 and "- [ ] Carry me" in target.read_text(encoding="utf-8"), result.stderr)
    check("checked task not carried", "Ship daily fast path" not in target.read_text(encoding="utf-8"))
    result = run(root, "sessions", "--date", "2026-07-11")
    target_text = target.read_text(encoding="utf-8")
    check("session map applied", result.returncode == 0 and "Wiki work ·" in target_text, result.stderr)
    check("session runtime heading applied", "### Codex\n\n- Wiki work" in target_text)
    session_map.write_text(
        "import sys\nprint(f'# no sessions on {sys.argv[1]}')\n",
        encoding="utf-8",
    )
    result = run(root, "sessions", "--date", "2026-07-12")
    empty_page = root / "wiki/daily/2026/07/2026-07-12.md"
    empty_text = empty_page.read_text(encoding="utf-8") if empty_page.is_file() else ""
    empty_session_section = (
        empty_text.split("## Сессии", 1)[1].split("## ", 1)[0].strip()
        if "## Сессии" in empty_text
        else "missing"
    )
    check(
        "empty session map applied",
        result.returncode == 0 and empty_session_section == "" and "# no sessions" not in empty_text,
        result.stderr,
    )
    result = run(root, "append", "--date", "2026-07-11", "--section", "notes", "--text", "## Injected heading")
    check("structural note injection rejected", result.returncode == 3 and "headings" in result.stderr)
    events = [json.loads(line) for line in (root / ".vault-meta/pipeline-events.jsonl").read_text(encoding="utf-8").splitlines()]
    check("journal uses canonical writer", all(event["actor"] == "journal" for event in events if event["op"] == "vault-write"))

print("\nAll journal writer tests passed.")
