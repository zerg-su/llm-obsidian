#!/usr/bin/env python3
"""Hermetic ownership tests for the v2.8.7 real-cmux acceptance probe."""

from __future__ import annotations

import importlib.util
import hashlib
import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "prototypes" / "v287_real_cmux_delivery.py"
SPEC = importlib.util.spec_from_file_location("v287_real_cmux_delivery_test", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)

WORKSPACE_ID = "11111111-1111-1111-1111-111111111111"
SURFACE_ID = "22222222-2222-2222-2222-222222222222"


def check(label: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(label)
    print(f"OK   {label}")


def tree_payload(*, surfaces: tuple[str, ...] = (SURFACE_ID,)) -> str:
    return json.dumps(
        {
            "windows": [
                {
                    "workspaces": [
                        {
                            "ref": "workspace:42",
                            "id": WORKSPACE_ID,
                            "panes": [
                                {
                                    "surfaces": [
                                        {"id": surface_id}
                                        for surface_id in surfaces
                                    ]
                                }
                            ],
                        }
                    ]
                }
            ]
        }
    )


original_run_cmux = probe.run_cmux
original_exact_ids = probe.exact_ids
original_wait_screen = probe.wait_screen
original_deliver = probe.deliver_continuation
original_run_runtime = probe.run_runtime
original_probe_identity = probe.probe_identity
try:
    replies = iter(("not-json", tree_payload()))
    probe.run_cmux = lambda *_argv: next(replies)
    observed = probe.exact_ids(
        "workspace:42", observation_limit=2, wait=lambda _seconds: None
    )
    check("delayed structured tree discovery resolves exact ids", observed == (WORKSPACE_ID, SURFACE_ID))

    for malformed in ("not-json", tree_payload(surfaces=(SURFACE_ID, SURFACE_ID))):
        probe.run_cmux = lambda *_argv, value=malformed: value
        try:
            probe.exact_ids(
                "workspace:42", observation_limit=1, wait=lambda _seconds: None
            )
        except RuntimeError:
            pass
        else:
            raise AssertionError("malformed or ambiguous identity must fail closed")
    check("malformed and ambiguous identities fail closed", True)

    try:
        probe.created_workspace_ref("OK without an exact reference")
    except RuntimeError:
        pass
    else:
        raise AssertionError("creation output without one exact ref must fail")
    check("creation output requires one exact workspace reference", True)

    commands: list[tuple[str, ...]] = []

    def identity_failure_command(*argv: str) -> str:
        commands.append(argv)
        if argv[0] == "new-workspace":
            return "OK workspace:42"
        if argv[0] == "close-workspace":
            return "OK"
        raise AssertionError(argv)

    probe.run_cmux = identity_failure_command
    probe.exact_ids = lambda _ref: (_ for _ in ()).throw(RuntimeError("identity"))
    try:
        probe.run_runtime(object(), "codex", 1, "owned-title")
    except RuntimeError as exc:
        check("identity discovery failure remains visible", str(exc) == "identity")
    else:
        raise AssertionError("identity failure must escape")
    check(
        "identity failure closes the exact creation reference",
        ("close-workspace", "--workspace", "workspace:42") in commands,
    )

    commands.clear()
    probe.run_cmux = identity_failure_command
    probe.exact_ids = lambda _ref: (WORKSPACE_ID, SURFACE_ID)
    probe.wait_screen = lambda *_args: (_ for _ in ()).throw(RuntimeError("child"))
    try:
        probe.run_runtime(object(), "claude", 1, "owned-title")
    except RuntimeError as exc:
        check("child failure remains visible", str(exc) == "child")
    else:
        raise AssertionError("child failure must escape")
    check(
        "child failure closes the discovered workspace identity",
        ("close-workspace", "--workspace", WORKSPACE_ID) in commands,
    )

    def close_failure_command(*argv: str) -> str:
        if argv[0] == "new-workspace":
            return "OK workspace:42"
        if argv[0] == "close-workspace":
            raise RuntimeError("close")
        raise AssertionError(argv)

    probe.run_cmux = close_failure_command
    probe.exact_ids = lambda _ref: (_ for _ in ()).throw(RuntimeError("identity"))
    try:
        probe.run_runtime(object(), "codex", 1, "owned-title")
    except RuntimeError as exc:
        check("close failure is never reported as successful cleanup", str(exc) == "close")
    else:
        raise AssertionError("close failure must escape")

    commands.clear()

    def concurrent_command(*argv: str) -> str:
        commands.append(argv)
        if argv[0] == "new-workspace":
            return "OK workspace:42"
        if argv[0] == "close-workspace":
            return "OK"
        if argv[:3] == ("workspace", "list", "--json"):
            return json.dumps(
                {
                    "workspaces": [
                        {
                            "title": "llm-obsidian-v287-codex-other",
                            "ref": "workspace:99",
                        }
                    ]
                }
            )
        raise AssertionError(argv)

    def accepted_delivery(*_args: object, **kwargs: object) -> object:
        kwargs["observe_stage"]("submit-accepted", 1, "", "")
        return SimpleNamespace(acknowledged=True, submit_count=1)

    probe.run_cmux = concurrent_command
    probe.exact_ids = lambda _ref: (WORKSPACE_ID, SURFACE_ID)
    probe.wait_screen = lambda *_args: "ready"
    probe.deliver_continuation = accepted_delivery
    count = probe.run_runtime(object(), "codex", 1, "owned-title")
    check("one successful runtime returns its exact turn count", count == 1)
    check(
        "another concurrent probe title is not mistaken for an owned tail",
        ("close-workspace", "--workspace", WORKSPACE_ID) in commands,
    )

    with tempfile.TemporaryDirectory(prefix="v287-receipt-test.") as raw:
        receipt = Path(raw) / "receipt.json"
        command = [
            sys.executable,
            str(SCRIPT),
            "--turns",
            "20",
            "--receipt",
            str(receipt),
        ]
        payload = probe.receipt_payload(
            "a" * 40,
            hashlib.sha256(SCRIPT.read_bytes()).hexdigest(),
            20,
            command,
        )
        probe.write_receipt(receipt, payload)
        observed_payload = json.loads(receipt.read_text(encoding="utf-8"))
        check("receipt binds the exact requested HEAD", observed_payload["head_sha"] == "a" * 40)
        check("receipt records 20 deliveries for both runtimes", observed_payload["runtime_counts"] == {"claude": 20, "codex": 20})
        check("receipt records zero tails and provider calls", observed_payload["workspace_tails"] == 0 and observed_payload["provider_calls"] == 0)
        check("receipt binds the exact probe bytes", observed_payload["probe_sha256"] == hashlib.sha256(SCRIPT.read_bytes()).hexdigest())
        check("receipt binds its replayable creating command", observed_payload["command"] == command)
        try:
            probe.write_receipt(receipt, payload)
        except RuntimeError:
            pass
        else:
            raise AssertionError("an immutable receipt target must not be overwritten")
        check("receipt publication is immutable", True)

    with tempfile.TemporaryDirectory(prefix="v287-cli-receipt-test.") as raw:
        receipt = Path(raw) / "receipt.json"
        stable_identity = ("b" * 40, hashlib.sha256(SCRIPT.read_bytes()).hexdigest())
        identities = iter((stable_identity, stable_identity))
        probe.probe_identity = lambda: next(identities)
        probe.run_runtime = lambda _adapter, _runtime, turns, _title: turns
        check(
            "CLI receipt run completes with provider-free runtime doubles",
            probe.main(["--turns", "1", "--receipt", str(receipt)]) == 0,
        )
        observed_payload = json.loads(receipt.read_text(encoding="utf-8"))
        check(
            "CLI receipt records the exact canonical creating invocation",
            observed_payload["command"]
            == [
                sys.executable,
                str(SCRIPT),
                "--turns",
                "1",
                "--receipt",
                str(receipt),
            ],
        )

    with tempfile.TemporaryDirectory(prefix="v287-head-drift-test.") as raw:
        receipt = Path(raw) / "receipt.json"
        identities = iter(
            (
                ("c" * 40, hashlib.sha256(SCRIPT.read_bytes()).hexdigest()),
                ("d" * 40, hashlib.sha256(SCRIPT.read_bytes()).hexdigest()),
            )
        )
        probe.probe_identity = lambda: next(identities)
        probe.run_runtime = lambda _adapter, _runtime, turns, _title: turns
        try:
            probe.parent(
                1,
                receipt,
                [sys.executable, str(SCRIPT), "--turns", "1", "--receipt", str(receipt)],
            )
        except RuntimeError as exc:
            check("HEAD drift remains a failed gate", "identity changed" in str(exc))
        else:
            raise AssertionError("HEAD drift must fail before receipt publication")
        check("HEAD drift publishes no complete receipt", not receipt.exists())
finally:
    probe.run_cmux = original_run_cmux
    probe.exact_ids = original_exact_ids
    probe.wait_screen = original_wait_screen
    probe.deliver_continuation = original_deliver
    probe.run_runtime = original_run_runtime
    probe.probe_identity = original_probe_identity

print("v2.8.7 real-cmux delivery ownership tests passed")
