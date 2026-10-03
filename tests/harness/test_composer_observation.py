#!/usr/bin/env python3
"""Logical-buffer authority is independent of rendered composer rows."""

import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
from harness.runtime_provider_input import interactive_provider_input
from harness.runtime_session_continuation import deliver_continuation, await_initial_input_visible

PROMPT = interactive_provider_input(
    "codex", Path("/tmp/" + "shared-directory/" * 8 + "expected.md"), "Expected contract",
)
OLD = interactive_provider_input(
    "codex", Path("/tmp/" + "shared-directory/" * 8 + "old.md"), "Older contract",
)
FOOTER = "\n\nGPT-6-Astra high · Context 5% used · 258K window · never\n? for shortcuts"


def render(text):
    return "› " + "\n  ".join(text[i:i + 120] for i in range(0, len(text), 120)) + FOOTER


class LogicalPort:
    def __init__(self, frames):
        self.frames = list(frames)
        self.current = ("", None)
        self.sent, self.keys = [], []

    def read(self, _surface):
        if self.frames:
            self.current = self.frames.pop(0)
        return self.current[0]

    def observe_composer(self, _surface):
        content = self.current[1]
        if content is None:
            return None
        return hashlib.sha256(content.encode()).hexdigest(), len(content.encode())

    def send(self, _surface, text):
        self.sent.append(text)

    def send_key(self, _surface, key):
        self.keys.append(key)


def deliver(frames):
    port = LogicalPort(frames)
    result = deliver_continuation(
        port, surface_id="probe", runtime="codex", prompt=PROMPT,
        artifact_ready=lambda: False, ownership_ready=lambda: True,
        reserve_retry=lambda: True, observe_stage=lambda *args: None,
        observation_limit=2, wait=lambda _seconds: None,
    )
    assert port.sent == [PROMPT]
    return result, port


result, port = deliver([(render(OLD), OLD), (render(PROMPT), PROMPT), ("• Working (1s)", None)])
assert result.acknowledged and port.keys == ["Enter"], (result, port.keys)
print("OK   exact logical change after shared first rendered row submits once")

for content in (PROMPT, OLD):
    result, port = deliver([(render(content), content)] * 5)
    assert not result.acknowledged and not port.keys
print("OK   unchanged logical buffer cannot acquire paste authority")

cut = PROMPT.index("expected.md") + len("expected")
for content in (
    PROMPT[:cut] + "\n" + PROMPT[cut:],
    PROMPT[:cut] + "\n\n" + PROMPT[cut:],
    PROMPT[:cut] + " " + PROMPT[cut:],
    "unrelated\n› " + PROMPT,
    PROMPT + "\n",
    OLD,
    None,
):
    # Deliberately identical screen, independently different logical source.
    result, port = deliver([(render(OLD), OLD), (render(PROMPT), content)] * 2)
    assert not result.acknowledged and not port.keys, (content, result, port.keys)
print("OK   hidden newlines/spaces/markers, wrong or unavailable buffers cannot authorize Enter")

result, port = deliver([
    (render(OLD), OLD), (render(PROMPT), PROMPT),
    (render(PROMPT), PROMPT), (render(PROMPT), PROMPT),
    (render(PROMPT), PROMPT[:cut] + "\n" + PROMPT[cut:]),
])
assert not result.acknowledged and port.keys == ["Enter"], (result, port.keys)
print("OK   retry obtains fresh logical authority despite an unchanged screen")

# Initial submission must use the same logical source as continuation.
for candidate, expected in ((PROMPT, True), (PROMPT + "\n", False), (OLD, False), (None, False)):
    initial = LogicalPort([(render(PROMPT), candidate)])
    assert await_initial_input_visible(
        initial, surface_id="probe", runtime="codex", text=PROMPT,
        before_editor_sha256=hashlib.sha256(OLD.encode()).hexdigest(),
        require_composer_authority=True, observation_limit=1, wait=lambda _: None,
    ) is expected
    assert not initial.keys and not initial.sent
print("OK   initial visibility shares exact logical authority and never derives it from rendered rows")

missing = LogicalPort([(render(OLD), None)])
result = deliver_continuation(
    missing, surface_id="probe", runtime="codex", prompt=PROMPT,
    artifact_ready=lambda: False, ownership_ready=lambda: True,
    reserve_retry=lambda: True, observe_stage=lambda *args: None,
    observation_limit=1, wait=lambda _: None,
)
assert not result.acknowledged and not missing.sent and not missing.keys
print("OK   unavailable baseline prevents both paste and Enter")

from harness.composer_observation import ComposerPort, configure_environment

with tempfile.TemporaryDirectory() as raw:
    runtime_root = Path(raw).resolve()
    seed = runtime_root / "editor-seed.md"
    original = "  literal buffer\n\npath with space.md\n " .encode()
    seed.write_bytes(original)
    seed.chmod(0o600)
    caller_env = {"VISUAL": "original-user-editor"}
    env = configure_environment({"runtime": "codex", "ready_path": runtime_root / "ready.json"}, caller_env)
    assert caller_env == {"VISUAL": "original-user-editor"}
    assert "VISUAL" in env and env["VISUAL"] != caller_env["VISUAL"]
    for excluded in (
        {"runtime": "claude", "ready_path": runtime_root / "ready.json"},
        {"runtime": "codex", "ready_path": runtime_root / "ready.json", "callback_mode": "research-fetch"},
        {"runtime": "codex", "ready_path": runtime_root / "ready.json", "callback_mode": "research-synth"},
    ):
        assert configure_environment(excluded, caller_env) is caller_env
    helper = ROOT / "scripts/harness/composer_observation.py"

    class EditorPort:
        def __init__(self):
            self.keys = []
            self.mode = "valid"

        def send_key(self, surface, key):
            assert surface == "probe" and key == "ctrl+g"
            self.keys.append(key)
            capture = runtime_root / "composer-observation"
            if self.mode == "stale":
                request = json.loads((capture / "request.json").read_text())
                request["nonce"] = "0" * 32
                receipt = capture / ("0" * 32 + ".json")
                receipt.write_text(json.dumps({**request, "sha256": "a" * 64, "byte_count": 4}))
                receipt.chmod(0o600)
                return
            if self.mode == "symlink":
                alias = runtime_root / "seed-link.md"
                alias.symlink_to(seed)
                selected = alias
            else:
                selected = seed
            result = subprocess.run([sys.executable, "-B", str(helper), str(capture), str(selected)], capture_output=True)
            assert result.returncode == 75 and not result.stdout and not result.stderr

        def read(self, _surface):
            return "› stable visual text" + FOOTER

    adapter = EditorPort()
    port = ComposerPort(adapter, runtime_root, os.getpgrp())
    assert port.observe_composer("probe") == (hashlib.sha256(original).hexdigest(), len(original))
    assert seed.read_bytes() == original
    assert not (port.root / "request.json").exists()
    assert not list(port.root.glob("[0-9a-f]*.json"))
    print("OK   real editor helper hashes literal bytes without bodies/global changes; nonce scratch reaped")

    for mode in ("symlink", "stale"):
        runtime_root = runtime_root / mode
        runtime_root.mkdir(mode=0o700)
        adapter.mode = mode
        port = ComposerPort(adapter, runtime_root, os.getpgrp())
        assert port.observe_composer("probe") is None and seed.read_bytes() == original
    print("OK   unsafe seed and stale receipt grant no logical authority")

with tempfile.TemporaryDirectory() as raw:
    runtime_root = Path(raw).resolve()
    seed = runtime_root / "late-seed.md"
    seed.write_bytes(b"old delayed draft")
    seed.chmod(0o600)

    class LateEditorPort:
        def __init__(self):
            self.keys = []
            self.first_request = None

        def send_key(self, _surface, key):
            assert key == "ctrl+g"
            self.keys.append(key)
            capture = runtime_root / "composer-observation"
            if self.first_request is None:
                self.first_request = json.loads((capture / "request.json").read_text())
                return  # A has not read its request or exited when the caller times out.
            # If B were admitted, the delayed A would read B's mutable request.
            late = subprocess.run([
                sys.executable, "-B", str(ROOT / "scripts/harness/composer_observation.py"),
                str(capture), str(seed),
            ], capture_output=True)
            assert late.returncode == 75

        def read(self, _surface):
            return "› idle draft"

    adapter = LateEditorPort()
    port = ComposerPort(adapter, runtime_root, os.getpgrp())
    assert port.observe_composer("probe") is None
    first = adapter.first_request
    assert port.observe_composer("probe") is None and adapter.keys == ["ctrl+g"]
    assert json.loads((port.root / "request.json").read_text()) == first
    late = subprocess.run([
        sys.executable, "-B", str(ROOT / "scripts/harness/composer_observation.py"),
        str(port.root), str(seed),
    ], capture_output=True)
    assert late.returncode == 75 and not late.stdout and not late.stderr
    receipt = json.loads((port.root / (first["nonce"] + ".json")).read_text())
    assert receipt["nonce"] == first["nonce"]
    assert receipt["sha256"] == hashlib.sha256(seed.read_bytes()).hexdigest()
    assert port.observe_composer("probe") is None and adapter.keys == ["ctrl+g"]
    print("OK   delayed helper retains only its original nonce; no later observation or submit authority")
