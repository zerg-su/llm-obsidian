#!/usr/bin/env python3
"""Public-seam tests for safe research and its cited artifact boundary."""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from harness.contracts import OperationSpec, RuntimeRoute
from harness.runtime_sessions import RuntimeSessionRequest
from harness.store import OperationStore
from harness.supervisor import OperationSupervisor
from harness.verification import load_profiles
from harness.workflows.research import (
    ResearchContext,
    ResearchOperationRequest,
    ResearchRequest,
    advance_research,
    enqueue,
    finalize_research,
    research_runtime_config,
    start_research,
)
from harness.workflows.research_contracts import (
    fetch_callback_payload,
    research_callback_identity,
)
from research_contract import (
    ResearchContractError,
    load_artifact,
    validate_result_artifact,
)


def check(label: str, value: bool) -> None:
    if not value:
        raise AssertionError(label)
    print(f"OK   {label}")


route = RuntimeRoute(
    "codex",
    "gpt-5.6-sol",
    "high",
    "research-safe",
    "a" * 64,
)
with tempfile.TemporaryDirectory(prefix="research-config.") as raw:
    config_root = Path(raw)
    fetch_config = research_runtime_config(
        "fetch", config_root, route, str(Path(sys.executable).resolve())
    )
    synth_config = research_runtime_config(
        "synth", config_root, route, str(Path(sys.executable).resolve())
    )
    check(
        "fetch isolation enables only its bounded network profile",
        'web_search = "live"' in fetch_config
        and "[features.network_proxy]\nenabled = true" in fetch_config
        and "[permissions.research-fetch.network]\nenabled = true"
        in fetch_config,
    )
    check(
        "synthesis isolation is networkless and historyless",
        'web_search = "disabled"' in synth_config
        and "[features.network_proxy]\nenabled = false" in synth_config
        and "[permissions.research-synth.network]\nenabled = false"
        in synth_config
        and 'history.persistence = "none"' in synth_config,
    )
context = ResearchContext(
    manifest="packets/research/manifest.json",
    request_sha256="b" * 64,
    scope="minimal",
)
request = ResearchOperationRequest(
    policy=ResearchRequest(
        operation_id="research-1",
        query_pointer="packets/research/question.md",
        context_manifest=context.manifest,
    ),
    owner_id="owner-1",
    route=route,
    context=context,
)
profiles = load_profiles(ROOT / "config/verification-profiles.toml")
check(
    "research verification profile is registered",
    "research-cited-artifact" in profiles,
)

with tempfile.TemporaryDirectory(prefix="research-workflow.") as raw:
    record = enqueue(request, OperationStore(Path(raw) / "state"))
    check(
        "safe research persists through OperationStore",
        record.spec.kind == "research"
        and record.spec.route.profile == "research-safe"
        and record.spec.context_manifest == context.manifest
        and record.spec.verification_profile == "research-cited-artifact",
    )
    check(
        "safe research identity is restart-stable",
        enqueue(request, OperationStore(Path(raw) / "state")) == record,
    )

for label, call in (
    (
        "safe research rejects full context",
        lambda: ResearchContext(
            manifest="packets/research/manifest.json",
            request_sha256="b" * 64,
            scope="full-explicit",
        ),
    ),
    (
        "unsafe research requires explicit authorization",
        lambda: ResearchRequest(
            operation_id="research-2",
            query_pointer="packets/research/question.md",
            context_manifest="packets/research/manifest.json",
            unsafe=True,
            context_scope="full-explicit",
        ),
    ),
):
    try:
        call()
    except ValueError:
        check(label, True)
    else:
        check(label, False)

explicit_unsafe = ResearchRequest(
    operation_id="research-3",
    query_pointer="packets/research/question.md",
    context_manifest="packets/research/manifest.json",
    unsafe=True,
    context_scope="full-explicit",
    unsafe_authorized=True,
)
check(
    "unsafe research is a distinct explicit route",
    explicit_unsafe.unsafe and explicit_unsafe.unsafe_authorized,
)

with tempfile.TemporaryDirectory(prefix="research-contract.") as raw:
    root = Path(raw)
    sources = root / "sources"
    sources.mkdir()
    source_body = "# Primary source\n\nBounded source text.\n"
    source_path = sources / "source-1.md"
    source_path.write_text(source_body, encoding="utf-8")
    source_sha = hashlib.sha256(source_body.encode()).hexdigest()
    artifact_path = root / "artifact.json"
    artifact_path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "run_id": "run-1",
                "request_sha256": "c" * 64,
                "fetched_at": "2026-07-30T00:00:00Z",
                "sources": [
                    {
                        "url": "https://example.com/primary",
                        "title": "Primary",
                        "content_path": "sources/source-1.md",
                        "content_sha256": source_sha,
                        "source_class": "official",
                    }
                ],
                "fetch_errors": [],
            }
        ),
        encoding="utf-8",
    )
    accepted = load_artifact(
        str(artifact_path),
        expected_run_id="run-1",
        expected_request_sha256="c" * 64,
    )
    check(
        "fetch artifact carries pointers instead of page bodies",
        accepted["sources"][0]["content_path"] == "sources/source-1.md"
        and "clean_markdown" not in accepted["sources"][0],
    )

    blank_error = json.loads(artifact_path.read_text(encoding="utf-8"))
    blank_error["fetch_errors"] = [""]
    artifact_path.write_text(json.dumps(blank_error), encoding="utf-8")
    try:
        load_artifact(str(artifact_path), expected_run_id="run-1")
    except ResearchContractError:
        check("strict artifact validation rejects blank fetch errors", True)
    else:
        check("strict artifact validation rejects blank fetch errors", False)
    artifact_path.write_text(
        json.dumps({**blank_error, "fetch_errors": []}),
        encoding="utf-8",
    )

    inline = json.loads(artifact_path.read_text(encoding="utf-8"))
    inline["sources"][0]["clean_markdown"] = source_body
    artifact_path.write_text(json.dumps(inline), encoding="utf-8")
    try:
        load_artifact(str(artifact_path), expected_run_id="run-1")
    except ResearchContractError:
        check("fetch artifact rejects inline page bodies", True)
    else:
        check("fetch artifact rejects inline page bodies", False)

    artifact_path.write_text(
        json.dumps({key: value for key, value in inline.items()}),
        encoding="utf-8",
    )
    inline["sources"][0].pop("clean_markdown")
    artifact_path.write_text(json.dumps(inline), encoding="utf-8")
    answer = "# Answer\n\nSupported claim. [Primary](https://example.com/primary)\n"
    answer_path = root / "answer.md"
    answer_path.write_text(answer, encoding="utf-8")
    complete = {
        "schema_version": 2,
        "run_id": "run-1",
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
    result = validate_result_artifact(
        complete,
        root=root,
        expected_run_id="run-1",
        source_urls={"https://example.com/primary"},
    )
    check(
        "synthesis returns one cited typed artifact",
        result["artifact"]["path"] == "answer.md"
        and len(result["artifact"]["citations"]) == 1,
    )


class FakeResearchRuntime:
    """External runtime double; workflow state remains the real OperationStore."""

    def __init__(self, store: OperationStore):
        self.store = store
        self.starts: list[object] = []
        self.exits: list[str] = []
        self.cleanups: list[str] = []

    def start(
        self, session_request: object, *, on_surface_opened: object = None
    ) -> object:
        del on_surface_opened
        if not isinstance(session_request, RuntimeSessionRequest):
            raise TypeError("research must use the generic runtime request")
        self.starts.append(session_request)
        spec = session_request.spec
        record = self.store.create(
            spec,
            lane_id=session_request.lane_id,
            run_id=session_request.run_id,
        )
        supervisor = OperationSupervisor(
            self.store, spec.owner_id, spec.operation_id
        )
        for state in ("preflight", "starting", "running", "awaiting-callback"):
            supervisor.transition(state)
        return SimpleNamespace(
            record=supervisor.read(),
            checkpoint="",
        )

    def request_exit(self, owner_id: str, operation_id: str) -> object:
        self.exits.append(operation_id)
        supervisor = OperationSupervisor(self.store, owner_id, operation_id)
        record = supervisor.read()
        if record.state != "finalizing":
            supervisor.transition("finalizing")
        supervisor.transition("exiting")
        return SimpleNamespace(record=supervisor.read())

    def cleanup(self, owner_id: str, operation_id: str) -> object:
        self.cleanups.append(operation_id)
        supervisor = OperationSupervisor(self.store, owner_id, operation_id)
        supervisor.transition("complete")
        return SimpleNamespace(record=supervisor.read())


def prepare_cancelled_fetch_fixture(
    root: Path,
    *,
    operation_id: str,
    owner_id: str,
    exact_callback_digest: bool,
) -> SimpleNamespace:
    """Build one real-store cancelled fetch with a durable callback receipt."""

    store = OperationStore(root / "state")
    runtime = FakeResearchRuntime(store)
    fetch = root / "fetch"
    synth = root / "synth"
    fetch_context = fetch / "context" / "packet"
    fetch_context.mkdir(parents=True)
    query = f"cancelled fetch recovery for {operation_id}"
    (fetch_context / "question.bin").write_text(query, encoding="utf-8")
    (fetch_context / "manifest.json").write_text(
        '{"schema_version":1}\n', encoding="utf-8"
    )
    (fetch / "fetch-prompt.md").write_text("fetch safely\n", encoding="utf-8")
    synth.mkdir()
    (synth / "synth-prompt.md").write_text(
        "synthesize safely\n", encoding="utf-8"
    )
    fetch_home = root / "fetch-home"
    synth_home = root / "synth-home"
    fetch_home.mkdir(mode=0o700)
    synth_home.mkdir(mode=0o700)
    request = ResearchOperationRequest(
        policy=ResearchRequest(
            operation_id=operation_id,
            query_pointer="context/packet/question.bin",
            context_manifest="context/packet/manifest.json",
        ),
        owner_id=owner_id,
        route=route,
        context=ResearchContext(
            manifest="context/packet/manifest.json",
            request_sha256=hashlib.sha256(query.encode()).hexdigest(),
        ),
    )
    execution = start_research(
        request,
        runtime,
        store,
        origin_surface="22222222-2222-4222-8222-222222222222",
        fetch_cwd=fetch,
        fetch_runtime_home=fetch_home,
        callback_wake="advance exact cancelled fetch",
    )
    source_body = "# Source\n\nDurable recovery evidence.\n"
    sources = fetch / "sources"
    sources.mkdir()
    (sources / "source-1.md").write_text(source_body, encoding="utf-8")
    fetch_artifact = {
        "schema_version": 2,
        "run_id": execution.fetch.run_id,
        "request_sha256": request.context.request_sha256,
        "fetched_at": "2026-08-04T00:00:00Z",
        "sources": [
            {
                "url": "https://example.com/durable",
                "title": "Durable",
                "content_path": "sources/source-1.md",
                "content_sha256": hashlib.sha256(
                    source_body.encode()
                ).hexdigest(),
                "source_class": "official",
            }
        ],
        "fetch_errors": [],
    }
    (fetch / "artifact.json").write_text(
        json.dumps(fetch_artifact), encoding="utf-8"
    )
    fetch_payload = {
        "stage": "fetch",
        "artifact_path": "artifact.json",
        "artifact_sha256": hashlib.sha256(
            (fetch / "artifact.json").read_bytes()
        ).hexdigest(),
        "source_count": 1,
    }
    payload_digest = hashlib.sha256(
        json.dumps(
            fetch_payload, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()
    accepted_fetch = store.read(owner_id, execution.fetch.spec.operation_id)
    store.save(
        replace(
            accepted_fetch,
            revision=accepted_fetch.revision + 1,
            accepted_callback_id=(
                f"research-fetch-{payload_digest[:24]}"
            ),
            accepted_callback_kind="research",
            accepted_callback_sha256=(
                payload_digest if exact_callback_digest else "0" * 64
            ),
        ),
        expected_revision=accepted_fetch.revision,
    )
    for state in ("cancelling", "exiting", "cancelled"):
        store.transition(
            owner_id,
            execution.fetch.spec.operation_id,
            state,
        )
    return SimpleNamespace(
        store=store,
        runtime=runtime,
        fetch=fetch,
        synth=synth,
        synth_home=synth_home,
        request=request,
        execution=execution,
        payload_digest=payload_digest,
    )


with tempfile.TemporaryDirectory(prefix="research-pipeline.") as raw:
    root = Path(raw)
    store = OperationStore(root / "state")
    runtime = FakeResearchRuntime(store)
    fetch = root / "fetch"
    synth = root / "synth"
    fetch_context = fetch / "context" / "packet"
    fetch_context.mkdir(parents=True)
    query = "bounded public question"
    query_path = fetch_context / "question.bin"
    query_path.write_text(query, encoding="utf-8")
    manifest_path = fetch_context / "manifest.json"
    manifest_path.write_text('{"schema_version":1}\n', encoding="utf-8")
    (fetch / "fetch-prompt.md").write_text("fetch safely\n", encoding="utf-8")
    (synth / "synth-prompt.md").parent.mkdir(parents=True)
    (synth / "synth-prompt.md").write_text("synthesize safely\n", encoding="utf-8")
    fetch_home = root / "fetch-home"
    synth_home = root / "synth-home"
    fetch_home.mkdir()
    synth_home.mkdir()
    fetch_home.chmod(0o700)
    synth_home.chmod(0o700)
    pipeline_request = ResearchOperationRequest(
        policy=ResearchRequest(
            operation_id="research-pipeline-1",
            query_pointer="context/packet/question.bin",
            context_manifest="context/packet/manifest.json",
        ),
        owner_id="owner-pipeline",
        route=route,
        context=ResearchContext(
            manifest="context/packet/manifest.json",
            request_sha256=hashlib.sha256(query.encode()).hexdigest(),
        ),
    )
    execution = start_research(
        pipeline_request,
        runtime,
        store,
        origin_surface="11111111-1111-4111-8111-111111111111",
        fetch_cwd=fetch,
        fetch_runtime_home=fetch_home,
        callback_wake="advance exact research pipeline",
    )
    check(
        "research workflow starts a harness-owned fetch stage",
        execution.stage == "fetch"
        and execution.parent.state == "awaiting-callback"
        and len(runtime.starts) == 1
        and runtime.starts[0].callback_mode == "research-fetch"
        and runtime.starts[0].runtime_home == fetch_home.resolve(),
    )
    expected_fetch_id = (
        "research-pipeline-1-fetch-"
        + hashlib.sha256(b"fetch").hexdigest()[:8]
    )
    fetch_request = runtime.starts[0]
    check(
        "research fetch has one derived generic operation identity",
        isinstance(execution.parent.spec, OperationSpec)
        and isinstance(execution.fetch.spec, OperationSpec)
        and execution.fetch.spec.operation_id == expected_fetch_id
        and execution.fetch.spec.parent_operation_id
        == execution.parent.spec.operation_id
        and fetch_request.spec == execution.fetch.spec
        and fetch_request.lane_id == execution.fetch.lane_id
        and fetch_request.run_id == execution.fetch.run_id
        and execution.parent.spec.owner_id == execution.fetch.spec.owner_id
        and len(store.list(pipeline_request.owner_id)) == 2,
    )
    check(
        "research fetch uses the generic typed callback seam",
        isinstance(fetch_request, RuntimeSessionRequest)
        and fetch_request.callback_mode == "research-fetch"
        and fetch_request.callback_pointer == "artifact.json"
        and fetch_request.research_request_sha256
        == pipeline_request.context.request_sha256
        and fetch_request.origin_surface
        == "11111111-1111-4111-8111-111111111111",
    )
    starts_before_early_poll = tuple(runtime.starts)
    try:
        advance_research(
            pipeline_request,
            runtime,
            store,
            origin_surface="11111111-1111-4111-8111-111111111111",
            fetch_cwd=fetch,
            synth_cwd=synth,
            synth_runtime_home=synth_home,
            callback_wake="finalize exact research pipeline",
        )
    except ValueError as exc:
        fetch_poll_error = str(exc)
    else:
        fetch_poll_error = ""
    check(
        "early fetch poll keeps the precise callback diagnostic",
        fetch_poll_error == "research fetch callback has not been accepted"
        and tuple(runtime.starts) == starts_before_early_poll,
    )

    source_body = "# Source\n\nBounded source text.\n"
    sources = fetch / "sources"
    sources.mkdir()
    (sources / "source-1.md").write_text(source_body, encoding="utf-8")
    fetch_artifact = {
        "schema_version": 2,
        "run_id": execution.fetch.run_id,
        "request_sha256": pipeline_request.context.request_sha256,
        "fetched_at": "2026-07-30T00:00:00Z",
        "sources": [
            {
                "url": "https://example.com/primary",
                "title": "Primary",
                "content_path": "sources/source-1.md",
                "content_sha256": hashlib.sha256(source_body.encode()).hexdigest(),
                "source_class": "official",
            }
        ],
        "fetch_errors": [],
    }
    (fetch / "artifact.json").write_text(json.dumps(fetch_artifact), encoding="utf-8")
    fetch_raw = (fetch / "artifact.json").read_bytes()
    fetch_payload = fetch_callback_payload(
        artifact_sha256=hashlib.sha256(fetch_raw).hexdigest(),
        source_count=1,
    )
    fetch_callback_id, fetch_payload_digest = research_callback_identity(
        fetch_payload
    )
    accepted_fetch = store.read(
        pipeline_request.owner_id, execution.fetch.spec.operation_id
    )
    store.save(
        replace(
            accepted_fetch,
            revision=accepted_fetch.revision + 1,
            accepted_callback_id=fetch_callback_id,
            accepted_callback_kind="research",
            accepted_callback_sha256=fetch_payload_digest,
        ),
        expected_revision=accepted_fetch.revision,
    )
    store.transition(
        pipeline_request.owner_id,
        execution.fetch.spec.operation_id,
        "finalizing",
    )
    execution = advance_research(
        pipeline_request,
        runtime,
        store,
        origin_surface="11111111-1111-4111-8111-111111111111",
        fetch_cwd=fetch,
        synth_cwd=synth,
        synth_runtime_home=synth_home,
        callback_wake="finalize exact research pipeline",
    )
    check(
        "accepted fetch completes before the separate synthesis stage",
        execution.stage == "synth"
        and execution.parent.state == "awaiting-callback"
        and execution.fetch.state == "complete"
        and execution.synth is not None
        and len(runtime.starts) == 2
        and runtime.starts[1].callback_mode == "research-synth"
        and runtime.exits == [execution.fetch.spec.operation_id]
        and runtime.cleanups == runtime.exits
        and (synth / "artifact.json").is_file()
        and (synth / "context" / "packet" / "manifest.json").is_file(),
    )
    expected_synth_id = (
        "research-pipeline-1-synth-"
        + hashlib.sha256(b"synth").hexdigest()[:8]
    )
    synth_request = runtime.starts[1]
    check(
        "research synthesis shares the store FSM and callback seam",
        isinstance(synth_request, RuntimeSessionRequest)
        and execution.synth is not None
        and execution.synth.spec.operation_id == expected_synth_id
        and execution.synth.spec.parent_operation_id
        == execution.parent.spec.operation_id
        and synth_request.spec == execution.synth.spec
        and synth_request.lane_id == execution.synth.lane_id
        and synth_request.run_id == execution.synth.run_id
        and synth_request.callback_mode == "research-synth"
        and synth_request.callback_pointer == "complete.json"
        and synth_request.research_request_sha256 == ""
        and len(store.list(pipeline_request.owner_id)) == 3,
    )
    exits_before_synth_poll = tuple(runtime.exits)
    try:
        finalize_research(
            pipeline_request,
            runtime,
            store,
            synth_cwd=synth,
        )
    except ValueError as exc:
        synth_poll_error = str(exc)
    else:
        synth_poll_error = ""
    check(
        "early synthesis poll keeps the precise callback diagnostic",
        synth_poll_error
        == "research synthesis callback has not been accepted"
        and tuple(runtime.exits) == exits_before_synth_poll,
    )
    provenance_path = (
        store.root
        / "owners"
        / pipeline_request.owner_id
        / "runtime"
        / execution.synth.spec.operation_id
        / "research-input.json"
    )
    provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    check(
        "advance pins only content-free synthesis input provenance",
        set(provenance)
        == {
            "schema_version",
            "operation_id",
            "run_id",
            "fetch_run_id",
            "request_sha256",
            "artifact_sha256",
        }
        and provenance["fetch_run_id"] == execution.fetch.run_id
        and provenance["artifact_sha256"]
        == hashlib.sha256((synth / "artifact.json").read_bytes()).hexdigest(),
    )
    replay_runtime = FakeResearchRuntime(store)
    replayed = advance_research(
        pipeline_request,
        replay_runtime,
        store,
        origin_surface="11111111-1111-4111-8111-111111111111",
        fetch_cwd=fetch,
        synth_cwd=synth,
        synth_runtime_home=synth_home,
        callback_wake="finalize exact research pipeline",
    )
    check(
        "restart replay does not repeat the accepted fetch or synth start",
        replayed.stage == "synth"
        and replayed.parent.state == "awaiting-callback"
        and replayed.fetch.state == "complete"
        and replayed.synth is not None
        and replayed.synth.spec == execution.synth.spec
        and replay_runtime.starts == []
        and replay_runtime.exits == []
        and replay_runtime.cleanups == []
        and len(store.list(pipeline_request.owner_id)) == 3,
    )

    answer = "# Answer\n\nSupported. [Primary](https://example.com/primary)\n"
    (synth / "answer.md").write_text(answer, encoding="utf-8")
    (synth / "complete.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "run_id": execution.synth.run_id,
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
        pipeline_request.owner_id,
        execution.synth.spec.operation_id,
        "finalizing",
    )
    original_synth_artifact = (synth / "artifact.json").read_bytes()
    original_synth_source = (synth / "sources/source-1.md").read_bytes()
    changed_source = "# Source\n\nChanged after the accepted fetch.\n"
    (synth / "sources/source-1.md").write_text(
        changed_source,
        encoding="utf-8",
    )
    changed_artifact = json.loads(original_synth_artifact)
    changed_artifact["sources"][0]["content_sha256"] = hashlib.sha256(
        changed_source.encode()
    ).hexdigest()
    (synth / "artifact.json").write_text(
        json.dumps(changed_artifact),
        encoding="utf-8",
    )
    try:
        finalize_research(
            pipeline_request,
            runtime,
            store,
            synth_cwd=synth,
        )
    except ResearchContractError:
        check(
            "finalization rejects a valid artifact that changed after advance",
            True,
        )
    else:
        check(
            "finalization rejects a valid artifact that changed after advance",
            False,
        )
    (synth / "artifact.json").write_bytes(original_synth_artifact)
    (synth / "sources/source-1.md").write_bytes(original_synth_source)
    completed = finalize_research(
        pipeline_request,
        runtime,
        store,
        synth_cwd=synth,
    )
    check(
        "research workflow returns one cited artifact after exact cleanup",
        completed.stage == "complete"
        and completed.parent.state == "complete"
        and completed.result_artifact is not None
        and completed.result_artifact["path"] == str((synth / "answer.md").resolve())
        and runtime.exits
        == [execution.fetch.spec.operation_id, execution.synth.spec.operation_id]
        and runtime.cleanups == runtime.exits,
    )

with tempfile.TemporaryDirectory(prefix="research-cancelled-fetch.") as raw:
    fixture = prepare_cancelled_fetch_fixture(
        Path(raw),
        operation_id="research-cancelled-fetch-1",
        owner_id="owner-cancelled-fetch",
        exact_callback_digest=False,
    )
    records_before_mismatch = tuple(
        fixture.store.list(fixture.request.owner_id)
    )
    synth_files_before_mismatch = tuple(
        sorted(path.name for path in fixture.synth.iterdir())
    )
    starts_before_mismatch = tuple(fixture.runtime.starts)
    try:
        advance_research(
            fixture.request,
            fixture.runtime,
            fixture.store,
            origin_surface="22222222-2222-4222-8222-222222222222",
            fetch_cwd=fixture.fetch,
            synth_cwd=fixture.synth,
            synth_runtime_home=fixture.synth_home,
            callback_wake="advance exact cancelled fetch",
        )
    except ValueError as exc:
        mismatch_error = str(exc)
    else:
        mismatch_error = ""
    check(
        "cancelled fetch digest mismatch starts no synthesis child or provider",
        mismatch_error == "research fetch callback has not been accepted"
        and fixture.runtime.exits == []
        and fixture.runtime.cleanups == []
        and tuple(fixture.runtime.starts) == starts_before_mismatch
        and tuple(fixture.store.list(fixture.request.owner_id))
        == records_before_mismatch
        and tuple(sorted(path.name for path in fixture.synth.iterdir()))
        == synth_files_before_mismatch,
    )
    mismatched_fetch = fixture.store.read(
        fixture.request.owner_id,
        fixture.execution.fetch.spec.operation_id,
    )
    fixture.store.save(
        replace(
            mismatched_fetch,
            revision=mismatched_fetch.revision + 1,
            accepted_callback_sha256=fixture.payload_digest,
        ),
        expected_revision=mismatched_fetch.revision,
    )
    recovered = advance_research(
        fixture.request,
        fixture.runtime,
        fixture.store,
        origin_surface="22222222-2222-4222-8222-222222222222",
        fetch_cwd=fixture.fetch,
        synth_cwd=fixture.synth,
        synth_runtime_home=fixture.synth_home,
        callback_wake="advance exact cancelled fetch",
    )
    check(
        "exact cancelled fetch receipt recovers only with a nonterminal parent",
        recovered.stage == "synth"
        and recovered.parent.state == "awaiting-callback"
        and recovered.fetch.state == "cancelled"
        and recovered.synth is not None
        and len(fixture.runtime.starts) == 2
        and fixture.runtime.starts[1].callback_mode == "research-synth"
        and fixture.runtime.exits == []
        and fixture.runtime.cleanups == []
        and len(fixture.store.list(fixture.request.owner_id)) == 3
        and (fixture.synth / "artifact.json").is_file(),
    )

with tempfile.TemporaryDirectory(prefix="research-terminal-parent.") as raw:
    fixture = prepare_cancelled_fetch_fixture(
        Path(raw),
        operation_id="research-terminal-parent-1",
        owner_id="owner-terminal-parent",
        exact_callback_digest=True,
    )
    for state in ("cancelling", "exiting", "cancelled"):
        fixture.store.transition(
            fixture.request.owner_id,
            fixture.execution.parent.spec.operation_id,
            state,
        )
    store_records_before = tuple(
        fixture.store.list(fixture.request.owner_id)
    )
    synth_files_before = tuple(
        sorted(path.name for path in fixture.synth.iterdir())
    )
    starts_before = tuple(fixture.runtime.starts)
    try:
        advance_research(
            fixture.request,
            fixture.runtime,
            fixture.store,
            origin_surface="22222222-2222-4222-8222-222222222222",
            fetch_cwd=fixture.fetch,
            synth_cwd=fixture.synth,
            synth_runtime_home=fixture.synth_home,
            callback_wake="do not resume terminal research",
        )
    except ValueError as exc:
        terminal_error = str(exc)
    else:
        terminal_error = ""
    check(
        "terminal research parent rejects recovery before child or provider effects",
        terminal_error == "terminal research composition cannot be resumed"
        and tuple(fixture.runtime.starts) == starts_before
        and fixture.runtime.exits == []
        and fixture.runtime.cleanups == []
        and tuple(fixture.store.list(fixture.request.owner_id))
        == store_records_before
        and tuple(sorted(path.name for path in fixture.synth.iterdir()))
        == synth_files_before,
    )

print("\nAll research vertical tests passed.")
