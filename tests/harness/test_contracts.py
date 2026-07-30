#!/usr/bin/env python3
"""Contract and migration-inventory baseline for the harness."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
from harness.contracts import (
    CallbackEnvelope,
    ContractError,
    OperationSpec,
    RuntimeRoute,
    to_dict,
)


def check(label: str, value: bool) -> None:
    if not value:
        raise AssertionError(label)
    print(f"OK   {label}")


digest = "a" * 64
route = RuntimeRoute("codex", "gpt-5.6-sol", "high", "executor", digest)
spec = OperationSpec("op-1", "key-1", "dispatch", "owner-1", route, "packet/manifest.json", "scoped")
encoded = json.dumps(to_dict(spec), sort_keys=True)
check("stable JSON serialization", json.loads(encoded)["route"]["runtime"] == "codex")
check("contracts are frozen", dataclasses.fields(spec) and spec.__dataclass_params__.frozen)

for label, factory in (
    ("unknown runtime rejected", lambda: RuntimeRoute("other", "model", "high", "executor", digest)),
    ("path escape rejected", lambda: OperationSpec("op", "key", "task", "owner", route, "../manifest", "scoped")),
    (
        "invalid compiled contract binding rejected",
        lambda: OperationSpec(
            "op",
            "key",
            "task",
            "owner",
            route,
            "manifest",
            "scoped",
            contract_sha256="not-a-digest",
        ),
    ),
    ("unknown schema rejected", lambda: OperationSpec("op", "key", "task", "owner", route, "manifest", "scoped", schema_version=2)),
):
    try:
        factory()
    except ContractError:
        check(label, True)
    else:
        check(label, False)

payload = {"verdict": "approve", "findings": []}
canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
callback = CallbackEnvelope("cb-1", "op-1", "run-1", "review", payload, hashlib.sha256(canonical).hexdigest())
try:
    callback.payload["extra"] = True
except TypeError:
    check("callback payload is immutable", True)
else:
    check("callback payload is immutable", False)

lint = subprocess.run(
    [sys.executable, str(ROOT / "scripts/runtime-harness-lint.py"), "--json"],
    cwd=ROOT, text=True, capture_output=True, check=False,
)
lint_value = json.loads(lint.stdout)
check(
    "runtime perimeter has no direct production callers",
    lint.returncode == 0
    and lint_value["direct_callers"] == {}
    and lint_value["unlisted"] == {},
)
strict = subprocess.run(
    [sys.executable, str(ROOT / "scripts/runtime-harness-lint.py"), "--strict", "--json"],
    cwd=ROOT, text=True, capture_output=True, check=False,
)
strict_value = json.loads(strict.stdout)
check(
    "strict runtime perimeter is clean",
    strict.returncode == 0
    and strict_value["direct_callers"] == {}
    and strict_value["stale_allowlist"] == {},
)

with tempfile.TemporaryDirectory(prefix="runtime-harness-lint.") as raw:
    fake = Path(raw)
    (fake / "scripts").mkdir()
    (fake / "skills").mkdir()
    (fake / "config").mkdir()
    (fake / "config/harness-direct-call-allowlist.json").write_text(
        '{"schema_version":1,"temporary_direct_callers":{}}\n',
        encoding="utf-8",
    )
    (fake / "scripts/dispatch-runner.py").write_text(
        'MARKER = "harness.workflows.dispatch.start_dispatch"\n'
        "def start():\n"
        "    return None\n",
        encoding="utf-8",
    )
    (fake / "scripts/reap-runner.py").write_text(
        '# run_reap is intentionally only a comment\n'
        "def apply_reap():\n"
        "    return None\n",
        encoding="utf-8",
    )
    (fake / "scripts/research-isolation.py").write_text(
        '# start_research is intentionally only a comment\n'
        "from task_sessions import TaskSessionStore\n"
        "def start():\n"
        '    parts = ["env", "CODEX_HOME=/tmp/isolated", "codex"]\n'
        "    return parts\n",
        encoding="utf-8",
    )
    (fake / "scripts/variable-runtime.py").write_text(
        "import os\n"
        "import shutil\n"
        "import subprocess\n"
        "from pathlib import Path\n"
        'configured = os.environ.get("CMUX_BUNDLED_CLI_PATH", "")\n'
        'runtime_binary = configured if configured else shutil.which("cmux")\n'
        'subprocess.run([runtime_binary, "send"], check=False)\n'
        'resolved_binary = str(Path("/opt/tools/codex").resolve())\n'
        'subprocess.run([resolved_binary, "--help"], check=False)\n',
        encoding="utf-8",
    )
    semantic = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/runtime-harness-lint.py"),
            "--root",
            str(fake),
            "--strict",
            "--json",
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    semantic_value = json.loads(semantic.stdout)
    check(
        "runtime lint requires executable production harness calls",
        semantic.returncode == 1
        and set(semantic_value["lifecycle_seam_violations"])
        == {
            "scripts/dispatch-runner.py",
            "scripts/reap-runner.py",
            "scripts/research-isolation.py",
        }
        and semantic_value["direct_callers"]
        == {
            "scripts/research-isolation.py": ["codex"],
            "scripts/variable-runtime.py": ["cmux", "codex"],
        },
    )
