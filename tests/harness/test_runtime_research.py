#!/usr/bin/env python3
"""Research callback transport through the generic runtime worker."""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from harness.adapters.process import ProcessAdapter
from harness.contracts import (
    AttentionReason,
    OperationSpec,
    RuntimeRoute,
)
from harness.runtime_worker import (
    _contain_provider_start_failure,
    _normalize_fetch_errors_at_provider_boundary,
    provider_argv,
    run as run_worker,
)
from harness.store import OperationStore
from research_contract import ResearchContractError, load_artifact


SURFACE = "11111111-1111-4111-8111-111111111111"
ORIGIN = "22222222-2222-4222-8222-222222222222"


def check(label: str, value: bool, detail: object = "") -> None:
    if not value:
        raise AssertionError(f"{label}: {detail}")
    print(f"OK   {label}")


class FakeCmux:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []
        self.keys: list[tuple[str, str]] = []

    def send(self, surface_id: str, text: str) -> None:
        self.sent.append((surface_id, text))

    def send_key(self, surface_id: str, key: str) -> None:
        self.keys.append((surface_id, key))

    def resume_checkpoint(self, _surface_id: str, _runtime: str) -> str:
        return "research-checkpoint"


def start_record(store: OperationStore, operation_id: str, run_id: str) -> None:
    route = RuntimeRoute(
        "codex",
        "gpt-5.6-sol",
        "high",
        "research-safe",
        "a" * 64,
    )
    store.create(
        OperationSpec(
            operation_id,
            f"{operation_id}-key",
            operation_id,
            "owner-research",
            route,
            "context/manifest.json",
            "research-cited-artifact",
        ),
        lane_id=f"{operation_id}-lane",
        run_id=run_id,
    )
    for state in ("preflight", "starting", "running", "awaiting-callback"):
        store.transition("owner-research", operation_id, state)


def launch(
    root: Path,
    store: OperationStore,
    *,
    operation_id: str,
    run_id: str,
    mode: str,
    request_sha256: str = "",
    tamper_artifact: bool = False,
    exit_code: int = 0,
) -> tuple[Path, Path]:
    cwd = root / operation_id
    cwd.mkdir()
    runtime_home = root / f"{operation_id}-home"
    runtime_home.mkdir(mode=0o700)
    callback = cwd / (
        "artifact.json" if mode == "research-fetch" else "complete.json"
    )
    env_marker = cwd / "codex-home.txt"
    provider = root / f"{operation_id}-provider.py"
    provider.write_text(
        "import hashlib,json,os,pathlib,sys,time\n"
        "keys=('CODEX_HOME','HOME','PATH','SCA_SECRET_SENTINEL',"
        "'CMUX_RESEARCH_SENTINEL')\n"
        "payload={key:os.environ.get(key) for key in keys}\n"
        "payload['CMUX_KEYS']=sorted(key for key in os.environ "
        "if key.startswith('CMUX_'))\n"
        "pathlib.Path(sys.argv[1]).write_text(json.dumps(payload), "
        "encoding='utf-8')\n"
        "if sys.argv[2] == 'tamper':\n"
        " root=pathlib.Path.cwd()\n"
        " source=root/'sources'/'one.md'\n"
        " changed='# Source\\n\\nTampered after synthesis launch.\\n'\n"
        " source.write_text(changed,encoding='utf-8')\n"
        " artifact=json.loads((root/'artifact.json').read_text(encoding='utf-8'))\n"
        " artifact['sources'][0]['content_sha256']=hashlib.sha256("
        "changed.encode()).hexdigest()\n"
        " (root/'artifact.json').write_text(json.dumps(artifact),encoding='utf-8')\n"
        " (root/'complete.pending.json').replace(root/'complete.json')\n"
        "time.sleep(0.2)\n"
        "sys.exit(int(sys.argv[3]))\n",
        encoding="utf-8",
    )
    prompt = cwd / "prompt.md"
    prompt.write_text("perform bounded research\n", encoding="utf-8")
    launch_spec = ProcessAdapter().prepare_surface_launch(
        argv=(
            str(Path(sys.executable).resolve()),
            str(provider),
            str(env_marker),
            "tamper" if tamper_artifact else "normal",
            str(exit_code),
        ),
        cwd=cwd,
        state_root=root / f"{operation_id}-state",
        worker=ROOT / "scripts" / "harness-runtime-worker.py",
        callback_pointer=callback,
        store_root=store.root,
        owner_id="owner-research",
        operation_id=operation_id,
        run_id=run_id,
        surface_id=SURFACE,
        runtime="codex",
        callback_mode=mode,
        origin_surface=ORIGIN,
        runtime_home=runtime_home,
        research_request_sha256=request_sha256,
        callback_wake=f"advance {operation_id}",
    )
    return launch_spec.spec_path, env_marker


with tempfile.TemporaryDirectory(prefix="runtime-shebang.") as raw:
    root = Path(raw)
    binary_root = root / "bin"
    binary_root.mkdir()
    node = binary_root / "node"
    node.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    node.chmod(0o755)
    codex = root / "codex.js"
    codex.write_text("#!/usr/bin/env node\n", encoding="utf-8")
    codex.chmod(0o755)
    command = provider_argv(
        {
            "argv": (str(codex), "--help"),
            "runtime": "codex",
            "surface_id": SURFACE,
        },
        env={"PATH": str(binary_root)},
    )
    check(
        "env shebang is pinned before the research PATH is sanitized",
        command == (str(node.resolve()), str(codex), "--help"),
        command,
    )

    wrapper_root = root / "cmux-cli-shims" / SURFACE
    wrapper_root.mkdir(parents=True)
    wrapper = wrapper_root / "codex"
    wrapper.write_text("#!/usr/bin/env bash\nexit 127\n", encoding="utf-8")
    wrapper.chmod(0o700)
    wrapper_env = {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "CMUX_SURFACE_ID": SURFACE,
        "CMUX_CODEX_WRAPPER_SHIM": str(wrapper),
        "CMUX_CODEX_WRAPPER_SHIM_ROOT": str(wrapper_root),
    }
    protected_command = provider_argv(
        {
            "argv": (str(codex), "--help"),
            "runtime": "codex",
            "surface_id": SURFACE,
            "callback_mode": "research-fetch",
            "runtime_interpreter": node.resolve(),
        },
        env=wrapper_env,
    )
    ordinary_command = provider_argv(
        {
            "argv": (str(codex), "--help"),
            "runtime": "codex",
            "surface_id": SURFACE,
            "callback_mode": "envelope",
            "runtime_interpreter": node.resolve(),
        },
        env=wrapper_env,
    )
    check(
        "protected research bypasses the cmux wrapper under sanitized PATH",
        protected_command
        == (str(node.resolve()), str(codex), "--help"),
        protected_command,
    )
    check(
        "ordinary runtimes retain the exact-surface cmux wrapper",
        ordinary_command == (str(wrapper.resolve()), "--help"),
        ordinary_command,
    )


with tempfile.TemporaryDirectory(prefix="runtime-research.") as raw:
    root = Path(raw)
    store = OperationStore(root / "store")
    fetch_id = "research-fetch"
    fetch_run = "run-fetch"
    request_sha256 = "b" * 64
    start_record(store, fetch_id, fetch_run)
    spec_path, env_marker = launch(
        root,
        store,
        operation_id=fetch_id,
        run_id=fetch_run,
        mode="research-fetch",
        request_sha256=request_sha256,
    )
    cwd = root / fetch_id
    sources = cwd / "sources"
    sources.mkdir()
    source = "# Source\n\nBounded public material.\n"
    (sources / "one.md").write_text(source, encoding="utf-8")
    artifact = {
        "schema_version": 2,
        "run_id": fetch_run,
        "request_sha256": request_sha256,
        "fetched_at": "2026-07-30T00:00:00Z",
        "sources": [
            {
                "url": "https://example.com/source",
                "title": "Source",
                "content_path": "sources/one.md",
                "content_sha256": hashlib.sha256(source.encode()).hexdigest(),
                "source_class": "official",
            }
        ],
        "fetch_errors": [
            "",
            " rate limited ",
            "   ",
            {
                "url": "https://example.com/rate-limit",
                "error": "temporary failure",
            },
        ],
    }
    raw_artifact = json.dumps(artifact, sort_keys=True).encode()
    (cwd / "artifact.json").write_bytes(raw_artifact)
    cmux = FakeCmux()
    with patch.dict(
        os.environ,
        {
            "SCA_SECRET_SENTINEL": "must-not-reach-research",
            "CMUX_RESEARCH_SENTINEL": "must-not-reach-research",
        },
    ):
        rc = run_worker(
            spec_path,
            poll_seconds=0.02,
            checkpoint_probe=cmux.resume_checkpoint,
            cmux_adapter=cmux,
        )
    record = store.read("owner-research", fetch_id)
    normalized_artifact_raw = (cwd / "artifact.json").read_bytes()
    normalized_artifact = json.loads(normalized_artifact_raw)
    fetch_payload = {
        "stage": "fetch",
        "artifact_path": "artifact.json",
        "artifact_sha256": hashlib.sha256(normalized_artifact_raw).hexdigest(),
        "source_count": 1,
    }
    fetch_payload_sha = hashlib.sha256(
        json.dumps(
            fetch_payload, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()
    check(
        "fetch callback is stable-read into one content-free receipt",
        rc == 0
        and record.state == "finalizing"
        and record.accepted_callback_kind == "research"
        and record.accepted_callback_sha256 == fetch_payload_sha
        and normalized_artifact["fetch_errors"]
        == [
            " rate limited ",
            "https://example.com/rate-limit: temporary failure",
        ]
        and cmux.sent == [(ORIGIN, f"advance {fetch_id}")]
        and cmux.keys == [(ORIGIN, "Enter")],
        (record, cmux.sent),
    )
    malformed_errors = [
        {"url": "https://example.com", "error": ""},
        {"url": "https://example.com"},
        {"url": "https://example.com", "error": "failed", "extra": "x"},
        {"message": "failed"},
        {"url": "u" * 1995, "error": "failed"},
        7,
        None,
    ]
    malformed_path = cwd / "malformed-artifact.json"
    malformed_artifact = {
        **normalized_artifact,
        "fetch_errors": malformed_errors,
    }
    malformed_raw = json.dumps(malformed_artifact, sort_keys=True).encode()
    malformed_path.write_bytes(malformed_raw)
    unchanged = _normalize_fetch_errors_at_provider_boundary(
        malformed_path,
        malformed_raw,
    )
    try:
        load_artifact(str(malformed_path))
    except ResearchContractError:
        malformed_rejected = True
    else:
        malformed_rejected = False
    check(
        "provider boundary preserves malformed fetch errors for strict rejection",
        unchanged == malformed_raw
        and json.loads(unchanged)["fetch_errors"] == malformed_errors
        and malformed_rejected,
    )
    fetch_env = json.loads(env_marker.read_text(encoding="utf-8"))
    check(
        "fetch receives only the bounded research environment",
        fetch_env["CODEX_HOME"]
        == str((root / f"{fetch_id}-home").resolve())
        and fetch_env["HOME"] == fetch_env["CODEX_HOME"]
        and fetch_env["PATH"] == "/usr/bin:/bin:/usr/sbin:/sbin"
        and fetch_env["SCA_SECRET_SENTINEL"] is None
        and fetch_env["CMUX_RESEARCH_SENTINEL"] is None
        and fetch_env["CMUX_KEYS"] == [],
        fetch_env,
    )
    duplicate_cmux = FakeCmux()
    duplicate_rc = run_worker(
        spec_path,
        poll_seconds=0.02,
        checkpoint_probe=duplicate_cmux.resume_checkpoint,
        cmux_adapter=duplicate_cmux,
    )
    check(
        "accepted research callback never duplicates its origin wake",
        duplicate_rc == 0
        and duplicate_cmux.sent == []
        and duplicate_cmux.keys == [],
    )

with tempfile.TemporaryDirectory(prefix="runtime-research-synth.") as raw:
    root = Path(raw)
    store = OperationStore(root / "store")
    synth_id = "research-synth"
    synth_run = "run-synth"
    start_record(store, synth_id, synth_run)
    spec_path, env_marker = launch(
        root,
        store,
        operation_id=synth_id,
        run_id=synth_run,
        mode="research-synth",
    )
    cwd = root / synth_id
    sources = cwd / "sources"
    sources.mkdir()
    source = "# Source\n\nBounded public material.\n"
    (sources / "one.md").write_text(source, encoding="utf-8")
    (cwd / "artifact.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "run_id": "run-fetch-parent",
                "request_sha256": "c" * 64,
                "fetched_at": "2026-07-30T00:00:00Z",
                "sources": [
                    {
                        "url": "https://example.com/source",
                        "title": "Source",
                        "content_path": "sources/one.md",
                        "content_sha256": hashlib.sha256(
                            source.encode()
                        ).hexdigest(),
                        "source_class": "official",
                    }
                ],
                "fetch_errors": [],
            }
        ),
        encoding="utf-8",
    )
    synth_input = json.loads(
        (cwd / "artifact.json").read_text(encoding="utf-8")
    )
    (spec_path.parent / "research-input.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "operation_id": synth_id,
                "run_id": synth_run,
                "fetch_run_id": synth_input["run_id"],
                "request_sha256": synth_input["request_sha256"],
                "artifact_sha256": hashlib.sha256(
                    (cwd / "artifact.json").read_bytes()
                ).hexdigest(),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )
    answer = "# Answer\n\n[Source](https://example.com/source)\n"
    (cwd / "answer.md").write_text(answer, encoding="utf-8")
    answer_sha = hashlib.sha256(answer.encode()).hexdigest()
    (cwd / "complete.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "run_id": synth_run,
                "status": "complete",
                "artifact": {
                    "kind": "cited-markdown",
                    "path": "answer.md",
                    "sha256": answer_sha,
                    "citations": [
                        {
                            "url": "https://example.com/source",
                            "title": "Source",
                            "source_class": "official",
                        }
                    ],
                },
            }
        ),
        encoding="utf-8",
    )
    cmux = FakeCmux()
    with patch.dict(
        os.environ,
        {
            "SCA_SECRET_SENTINEL": "must-not-reach-research",
            "CMUX_RESEARCH_SENTINEL": "must-not-reach-research",
        },
    ):
        rc = run_worker(
            spec_path,
            poll_seconds=0.02,
            checkpoint_probe=cmux.resume_checkpoint,
            cmux_adapter=cmux,
        )
    synth_payload = {
        "stage": "synth",
        "artifact_path": "answer.md",
        "artifact_sha256": answer_sha,
        "citation_count": 1,
    }
    synth_payload_sha = hashlib.sha256(
        json.dumps(
            synth_payload, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()
    record = store.read("owner-research", synth_id)
    check(
        "synth callback validates citations without storing their content",
        rc == 0
        and record.state == "finalizing"
        and record.accepted_callback_sha256 == synth_payload_sha
        and cmux.sent == [(ORIGIN, f"advance {synth_id}")],
        record,
    )
    synth_env = json.loads(env_marker.read_text(encoding="utf-8"))
    check(
        "synth receives only the bounded research environment",
        synth_env["CODEX_HOME"]
        == str((root / f"{synth_id}-home").resolve())
        and synth_env["HOME"] == synth_env["CODEX_HOME"]
        and synth_env["PATH"] == "/usr/bin:/bin:/usr/sbin:/sbin"
        and synth_env["SCA_SECRET_SENTINEL"] is None
        and synth_env["CMUX_RESEARCH_SENTINEL"] is None
        and synth_env["CMUX_KEYS"] == [],
        synth_env,
    )

with tempfile.TemporaryDirectory(
    prefix="runtime-research-synth-provenance."
) as raw:
    root = Path(raw)
    store = OperationStore(root / "store")
    synth_id = "research-synth-provenance"
    synth_run = "run-synth-provenance"
    start_record(store, synth_id, synth_run)
    spec_path, _env_marker = launch(
        root,
        store,
        operation_id=synth_id,
        run_id=synth_run,
        mode="research-synth",
        tamper_artifact=True,
    )
    cwd = root / synth_id
    sources = cwd / "sources"
    sources.mkdir()
    source = "# Source\n\nAdvance-validated material.\n"
    (sources / "one.md").write_text(source, encoding="utf-8")
    (cwd / "artifact.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "run_id": "run-fetch-provenance",
                "request_sha256": "e" * 64,
                "fetched_at": "2026-07-30T00:00:00Z",
                "sources": [
                    {
                        "url": "https://example.com/source",
                        "title": "Source",
                        "content_path": "sources/one.md",
                        "content_sha256": hashlib.sha256(
                            source.encode()
                        ).hexdigest(),
                        "source_class": "official",
                    }
                ],
                "fetch_errors": [],
            }
        ),
        encoding="utf-8",
    )
    answer = "# Answer\n\n[Source](https://example.com/source)\n"
    (cwd / "answer.md").write_text(answer, encoding="utf-8")
    (cwd / "complete.pending.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "run_id": synth_run,
                "status": "complete",
                "artifact": {
                    "kind": "cited-markdown",
                    "path": "answer.md",
                    "sha256": hashlib.sha256(answer.encode()).hexdigest(),
                    "citations": [
                        {
                            "url": "https://example.com/source",
                            "title": "Source",
                            "source_class": "official",
                        }
                    ],
                },
            }
        ),
        encoding="utf-8",
    )
    cmux = FakeCmux()
    run_worker(
        spec_path,
        poll_seconds=0.02,
        checkpoint_probe=cmux.resume_checkpoint,
        cmux_adapter=cmux,
    )
    record = store.read("owner-research", synth_id)
    check(
        "synth rejects a valid artifact swapped after provider launch",
        record.state == "attention-required"
        and record.attention_reason == AttentionReason.CALLBACK_INVALID
        and cmux.sent == [],
        record,
    )

with tempfile.TemporaryDirectory(prefix="runtime-research-invalid.") as raw:
    root = Path(raw)
    store = OperationStore(root / "store")
    invalid_id = "research-invalid"
    invalid_run = "run-invalid"
    start_record(store, invalid_id, invalid_run)
    spec_path, _env_marker = launch(
        root,
        store,
        operation_id=invalid_id,
        run_id=invalid_run,
        mode="research-fetch",
        request_sha256="d" * 64,
    )
    (root / invalid_id / "artifact.json").write_text(
        '{"schema_version":2,"inline_body":"forbidden"}',
        encoding="utf-8",
    )
    cmux = FakeCmux()
    run_worker(
        spec_path,
        poll_seconds=0.02,
        checkpoint_probe=cmux.resume_checkpoint,
        cmux_adapter=cmux,
    )
    record = store.read("owner-research", invalid_id)
    check(
        "invalid research artifact fails closed without waking origin",
        record.state == "attention-required"
        and record.attention_reason == AttentionReason.CALLBACK_INVALID
        and cmux.sent == [],
        record,
    )

with tempfile.TemporaryDirectory(prefix="runtime-start-failure.") as raw:
    root = Path(raw)
    store = OperationStore(root / "store")
    operation_id = "research-start-failure"
    run_id = "run-start-failure"
    start_record(store, operation_id, run_id)
    spec_path, _env_marker = launch(
        root,
        store,
        operation_id=operation_id,
        run_id=run_id,
        mode="research-fetch",
        request_sha256="e" * 64,
    )
    real_capture = ProcessAdapter.capture_identity

    def fail_supervisor_identity(
        pid: int, *, process_group: int = 0
    ) -> str:
        if pid == os.getpid():
            return ""
        return real_capture(pid, process_group=process_group)

    with (
        patch.object(
            ProcessAdapter,
            "capture_identity",
            side_effect=fail_supervisor_identity,
        ),
        patch(
            "harness.runtime_worker._contain_provider_start_failure",
            wraps=_contain_provider_start_failure,
        ) as contain,
    ):
        rc = run_worker(
            spec_path,
            poll_seconds=0.02,
            checkpoint_probe=FakeCmux().resume_checkpoint,
            cmux_adapter=FakeCmux(),
        )
    contained_handle = contain.call_args.args[1]
    check(
        "supervisor identity failure contains and reaps its provider",
        rc == 127
        and contain.call_count == 1
        and ProcessAdapter.process_status(
            contained_handle.process_group,
            contained_handle.process_identity,
        )
        == "dead",
    )

with tempfile.TemporaryDirectory(prefix="runtime-provider-exit.") as raw:
    root = Path(raw)
    store = OperationStore(root / "store")
    operation_id = "research-provider-exit"
    run_id = "run-provider-exit"
    start_record(store, operation_id, run_id)
    spec_path, _env_marker = launch(
        root,
        store,
        operation_id=operation_id,
        run_id=run_id,
        mode="research-fetch",
        request_sha256="f" * 64,
        exit_code=23,
    )
    rc = run_worker(
        spec_path,
        poll_seconds=0.02,
        checkpoint_probe=FakeCmux().resume_checkpoint,
        cmux_adapter=FakeCmux(),
    )
    record = store.read("owner-research", operation_id)
    check(
        "nonzero research provider exit becomes immediate typed attention",
        rc == 23
        and record.state == "attention-required"
        and record.attention_reason == AttentionReason.ATTENTION_REQUIRED,
        record,
    )
