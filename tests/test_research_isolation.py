#!/usr/bin/env python3
"""Thin protected-research CLI delegates every lifecycle effect to the harness."""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import runpy
import shlex
import sys
import tempfile
import uuid
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from harness.store import OperationStore


module = runpy.run_path(str(ROOT / "scripts/research-isolation.py"))
main = module["main"]
parser = module["parser"]


def check(label: str, value: bool) -> None:
    if not value:
        raise AssertionError(label)
    print(f"OK   {label}")


class FakeRuntime:
    def __init__(self, store: OperationStore):
        self.store = store
        self.starts: list[object] = []
        self.exits: list[str] = []
        self.cleanups: list[str] = []

    def start(
        self, request: object, *, on_surface_opened: object = None
    ) -> object:
        del on_surface_opened
        self.starts.append(request)
        record = self.store.create(
            request.spec,
            lane_id=request.lane_id,
            run_id=request.run_id,
        )
        for state in ("preflight", "starting", "running", "awaiting-callback"):
            self.store.transition(
                request.spec.owner_id,
                request.spec.operation_id,
                state,
            )
        return SimpleNamespace(
            record=self.store.read(
                request.spec.owner_id, request.spec.operation_id
            ),
            checkpoint="",
        )

    def request_exit(self, owner_id: str, operation_id: str) -> object:
        self.exits.append(operation_id)
        current = self.store.read(owner_id, operation_id)
        if current.state != "finalizing":
            self.store.transition(owner_id, operation_id, "finalizing")
        self.store.transition(owner_id, operation_id, "exiting")
        return SimpleNamespace(record=self.store.read(owner_id, operation_id))

    def cleanup(self, owner_id: str, operation_id: str) -> object:
        self.cleanups.append(operation_id)
        self.store.transition(owner_id, operation_id, "complete")
        return SimpleNamespace(record=self.store.read(owner_id, operation_id))


def invoke(
    argv: list[str],
    *,
    runtime: FakeRuntime,
    store: OperationStore,
) -> dict[str, object]:
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        result = main(
            argv,
            runtime_factory=lambda _vault, _store: runtime,
            store_factory=lambda _root: store,
        )
    assert result == 0
    return json.loads(output.getvalue())


with tempfile.TemporaryDirectory(prefix="research-cli.") as raw:
    temp = Path(raw)
    store_root = temp / "harness"
    store = OperationStore(store_root)
    runtime = FakeRuntime(store)
    owner = "owner-cli"
    operation_id = str(uuid.uuid4())
    surface = "11111111-1111-4111-8111-111111111111"
    env_before = {
        key: os.environ.get(key)
        for key in (
            "LLM_OBSIDIAN_SESSION_RUNTIME",
            "LLM_OBSIDIAN_SESSION_MODEL",
            "LLM_OBSIDIAN_SESSION_EFFORT",
        )
    }
    os.environ.update(
        {
            "LLM_OBSIDIAN_SESSION_RUNTIME": "codex",
            "LLM_OBSIDIAN_SESSION_MODEL": "gpt-5.6-sol",
            "LLM_OBSIDIAN_SESSION_EFFORT": "high",
        }
    )
    common = [
        "--vault-root",
        str(ROOT),
        "--store-root",
        str(store_root),
    ]
    start_argv = [
        *common,
        "start",
        "--flow",
        "research",
        "--topic",
        "bounded CLI question",
        "--operation-id",
        operation_id,
        "--owner",
        owner,
        "--coordinator-surface",
        surface,
    ]
    started = invoke(
        start_argv,
        runtime=runtime,
        store=store,
    )
    check(
        "thin CLI starts only the harness fetch stage",
        started["stage"] == "fetch"
        and started["status"] == "awaiting-callback"
        and len(runtime.starts) == 1
        and runtime.starts[0].callback_mode == "research-fetch",
    )
    wake_argv = shlex.split(
        runtime.starts[0].callback_wake.split("Run: ", 1)[1]
    )
    parsed_wake = parser().parse_args(wake_argv[2:])
    check(
        "generated callback command keeps global options before advance",
        parsed_wake.command == "advance"
        and parsed_wake.vault_root == ROOT.resolve()
        and parsed_wake.store_root == store_root.resolve()
        and parsed_wake.operation_id == operation_id
        and parsed_wake.owner == owner,
    )
    replayed_start = invoke(
        start_argv,
        runtime=runtime,
        store=store,
    )
    check(
        "same start request is restart-safe",
        replayed_start == started and len(runtime.starts) == 1,
    )
    source_body = "# Primary\n\nBounded evidence.\n"
    pipeline = store_root / "owners" / owner / "research" / operation_id
    fetch = pipeline / "fetch"
    sources = fetch / "sources"
    sources.mkdir()
    (sources / "source-1.md").write_text(source_body, encoding="utf-8")
    request_sha = hashlib.sha256(b"bounded CLI question").hexdigest()
    fetch_run = str(started["fetch"]["run_id"])
    (fetch / "artifact.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "run_id": fetch_run,
                "request_sha256": request_sha,
                "fetched_at": "2026-07-30T00:00:00Z",
                "sources": [
                    {
                        "url": "https://example.com/primary",
                        "title": "Primary",
                        "content_path": "sources/source-1.md",
                        "content_sha256": hashlib.sha256(
                            source_body.encode()
                        ).hexdigest(),
                        "source_class": "official",
                    }
                ],
                "fetch_errors": [],
            }
        ),
        encoding="utf-8",
    )
    store.transition(
        owner,
        str(started["fetch"]["operation_id"]),
        "finalizing",
    )
    advanced = invoke(
        [
            *common,
            "advance",
            "--operation-id",
            operation_id,
            "--owner",
            owner,
            "--coordinator-surface",
            surface,
        ],
        runtime=runtime,
        store=store,
    )
    check(
        "CLI advances through the executable research workflow",
        advanced["stage"] == "synth"
        and advanced["fetch"]["status"] == "complete"
        and advanced["synth"]["status"] == "awaiting-callback"
        and len(runtime.starts) == 2
        and runtime.starts[1].callback_mode == "research-synth",
    )
    exits_after_advance = list(runtime.exits)
    cleanups_after_advance = list(runtime.cleanups)
    replayed_advance = invoke(
        [
            *common,
            "advance",
            "--operation-id",
            operation_id,
            "--owner",
            owner,
            "--coordinator-surface",
            surface,
        ],
        runtime=runtime,
        store=store,
    )
    check(
        "replayed advance waits for the active synth callback",
        replayed_advance == advanced
        and len(runtime.starts) == 2
        and runtime.exits == exits_after_advance
        and runtime.cleanups == cleanups_after_advance,
    )
    synth = pipeline / "synth"
    answer = "# Answer\n\nSupported. [Primary](https://example.com/primary)\n"
    (synth / "answer.md").write_text(answer, encoding="utf-8")
    (synth / "complete.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "run_id": advanced["synth"]["run_id"],
                "status": "complete",
                "artifact": {
                    "kind": "cited-markdown",
                    "path": "answer.md",
                    "sha256": hashlib.sha256(answer.encode()).hexdigest(),
                    "citations": [
                        {
                            "url": "https://example.com/primary",
                            "title": "Primary",
                            "source_class": "official",
                        }
                    ],
                },
            }
        ),
        encoding="utf-8",
    )
    store.transition(
        owner,
        str(advanced["synth"]["operation_id"]),
        "finalizing",
    )
    completed = invoke(
        [
            *common,
            "advance",
            "--operation-id",
            operation_id,
            "--owner",
            owner,
            "--coordinator-surface",
            surface,
        ],
        runtime=runtime,
        store=store,
    )
    check(
        "CLI returns one cited artifact after harness cleanup",
        completed["stage"] == "complete"
        and completed["status"] == "complete"
        and completed["result_artifact"]["path"]
        == str((synth / "answer.md").resolve())
        and runtime.exits == runtime.cleanups
        and len(runtime.exits) == 2,
    )
    status = invoke(
        [
            *common,
            "status",
            "--operation-id",
            operation_id,
            "--owner",
            owner,
        ],
        runtime=runtime,
        store=store,
    )
    check(
        "status is a read-only harness ledger view",
        status["stage"] == "complete"
        and status["result_artifact"] == completed["result_artifact"]
        and len(runtime.starts) == 2,
    )
    for key, value in env_before.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value

source = (ROOT / "scripts/research-isolation.py").read_text(encoding="utf-8")
for forbidden in (
    "TaskSessionStore",
    "CmuxAdapter",
    "ProcessAdapter",
    '["env"',
    "launch-agent.sh",
):
    check(f"thin CLI excludes legacy runtime token {forbidden}", forbidden not in source)

print("\nAll thin research CLI tests passed.")
