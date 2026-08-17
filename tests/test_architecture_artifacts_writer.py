#!/usr/bin/env python3
"""Exercise project artifacts through the real vault-write CLI in a fixture."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class Suite:
    def __init__(self) -> None:
        self.failures: list[str] = []

    def check(self, label: str, condition: bool, detail: object = "") -> None:
        if condition:
            print(f"OK   {label}")
        else:
            print(f"FAIL {label}: {detail}")
            self.failures.append(label)


def artifact(title: str, role: str, address: str, body: str) -> str:
    return f'''---
type: project
title: "{title}"
artifact_role: {role}
project_key: atlas
project_display_name: Atlas
artifact_revision: 1
upstream:
  - "[[Atlas Architecture]]"
upstream_pins:
  - "Atlas Architecture@1"
depends_on: []
status: accepted
created: 2026-08-17
updated: 2026-08-17
tags: [project, architecture-workflow]
sessions:
  - fixture-session
address: {address}
---

# {title}

{body}
'''


def install_writer(root: Path) -> Path:
    scripts = root / "scripts"
    scripts.mkdir()
    for source in sorted((ROOT / "scripts").glob("vault_write*.py")):
        shutil.copy2(source, scripts / source.name)
    for name in (
        "vault-write.py",
        "vault_schema.py",
        "pipeline_events.py",
        "plan_lifecycle.py",
    ):
        source = ROOT / "scripts" / name
        shutil.copy2(source, scripts / name)
    (root / ".vault-meta").mkdir()
    (root / "wiki").mkdir()
    return scripts / "vault-write.py"


def run_writer(writer: Path, payload: dict | None = None, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(writer), *args],
        cwd=writer.parents[1],
        input=json.dumps(payload) if payload is not None else None,
        text=True,
        capture_output=True,
        check=False,
    )


def main() -> int:
    suite = Suite()
    with tempfile.TemporaryDirectory(prefix="architecture-writer.") as raw:
        root = Path(raw).resolve()
        writer = install_writer(root)
        wi_path = "wiki/projects/atlas/work/Atlas WI-001 — Recovery.md"
        original = artifact(
            "Atlas WI-001 — Recovery", "work-item", "c-123451", "Original outcome."
        )
        created = run_writer(
            writer,
            {
                "schema_version": 1,
                "actor": "architecture-workflow-fixture",
                "session": "fixture-session",
                "pages": [{"op": "create", "path": wi_path, "content": original}],
            },
        )
        target = root / wi_path
        suite.check(
            "vault-write accepts project metadata and nested project paths",
            created.returncode == 0 and target.read_text(encoding="utf-8") == original,
            created.stderr,
        )

        updated_text = original.replace("Original outcome.", "Updated outcome.")
        stale = run_writer(
            writer,
            {
                "pages": [
                    {
                        "op": "update",
                        "path": wi_path,
                        "expected_sha256": "0" * 64,
                        "content": updated_text,
                    }
                ]
            },
        )
        suite.check(
            "stale project update is rejected without mutation",
            stale.returncode == 4 and target.read_text(encoding="utf-8") == original,
            stale.stderr,
        )
        expected = hashlib.sha256(original.encode()).hexdigest()
        updated = run_writer(
            writer,
            {
                "pages": [
                    {
                        "op": "update",
                        "path": wi_path,
                        "expected_sha256": expected,
                        "content": updated_text,
                    }
                ]
            },
        )
        suite.check(
            "matching expected_sha256 updates the project artifact",
            updated.returncode == 0
            and target.read_text(encoding="utf-8") == updated_text,
            updated.stderr,
        )

        escaped = run_writer(
            writer,
            {
                "pages": [
                    {
                        "op": "create",
                        "path": "wiki/../outside.md",
                        "content": artifact(
                            "Atlas Outside", "spec", "c-123452", "Must not land."
                        ),
                    }
                ]
            },
        )
        suite.check(
            "generic writer confinement rejects paths outside wiki",
            escaped.returncode == 3 and not (root / "outside.md").exists(),
            escaped.stderr,
        )

        graph_path = "wiki/projects/atlas/work/Atlas Work Graph.md"
        second_path = "wiki/projects/atlas/work/Atlas WI-002 — Delivery.md"
        crash_payload = {
            "actor": "architecture-workflow-crash-fixture",
            "pages": [
                {
                    "op": "create",
                    "path": graph_path,
                    "content": artifact(
                        "Atlas Work Graph", "work-graph", "c-123453", "Projection."
                    ),
                },
                {
                    "op": "create",
                    "path": second_path,
                    "content": artifact(
                        "Atlas WI-002 — Delivery",
                        "work-item",
                        "c-123454",
                        "Delivery outcome.",
                    ),
                },
            ],
        }
        crash_code = r'''
import importlib.util
import io
import json
import os
import sys
from pathlib import Path

writer = Path(sys.argv[1])
sys.path.insert(0, str(writer.parent))
spec = importlib.util.spec_from_file_location("architecture_writer_crash", writer)
module = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(module)
original = module.atomic_write

def stop_after_journal(path, text):
    if path == module.JOURNAL_FILE:
        original(path, text)
        return
    os._exit(99)

module.atomic_write = stop_after_journal
sys.stdin = io.StringIO(json.dumps(json.loads(sys.argv[2])))
raise SystemExit(module.main([]))
'''
        crashed = subprocess.run(
            [sys.executable, "-c", crash_code, str(writer), json.dumps(crash_payload)],
            cwd=root,
            text=True,
            capture_output=True,
            check=False,
        )
        journal = root / ".vault-meta" / ".vault-write-journal.json"
        suite.check(
            "interrupted multi-page transaction leaves a durable journal",
            crashed.returncode == 99
            and journal.is_file()
            and not (root / graph_path).exists()
            and not (root / second_path).exists(),
            crashed.stderr,
        )
        recovered = run_writer(writer, None, "--recover")
        suite.check(
            "the next writer invocation rolls the complete projection forward",
            recovered.returncode == 0
            and (root / graph_path).is_file()
            and (root / second_path).is_file()
            and not journal.exists(),
            recovered.stderr,
        )

    return int(bool(suite.failures))


if __name__ == "__main__":
    raise SystemExit(main())
