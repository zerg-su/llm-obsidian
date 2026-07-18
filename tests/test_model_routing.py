#!/usr/bin/env python3
"""Hermetic contract tests for session-aware model routing."""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import model_routing as routing


def check(name: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(name)
    print(f"OK   {name}")


with tempfile.TemporaryDirectory(prefix="model-routing-test.") as raw:
    root = Path(raw)
    (root / "config").mkdir()
    (root / ".codex/profiles").mkdir(parents=True)
    (root / ".codex/agents").mkdir(parents=True)
    shutil.copy2(ROOT / "config/model-routing.toml", root / "config/model-routing.toml")
    for rel in (
        ".codex/config.toml", ".codex/profiles/default.toml",
        ".codex/profiles/wiki-write.toml", ".codex/profiles/reviewer-readonly.toml",
        ".codex/profiles/deep.toml", ".codex/agents/daily-summarizer.toml",
    ):
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / rel, target)

    config = routing.load_config(root)
    codex = {"runtime": "codex", "model": "gpt-5.6-sol", "effort": "high"}
    claude = {"runtime": "claude", "model": "opus", "effort": "high"}

    check("Claude runtime default is Opus", config.runtime_default("claude")["model"] == "opus")
    check("Claude reviewer default is Fable", config.reviewer_default("claude")["model"] == "fable")
    check("all concrete defaults are discoverable", config.default_models() == {"gpt-5.6-sol", "opus", "fable"})

    route = routing.resolve(config, "dispatch", session=codex)
    check("dispatch inherits exact session", (route["runtime"], route["model"], route["effort"]) == ("codex", "gpt-5.6-sol", "high"))
    route = routing.resolve(config, "daily", session=claude)
    check("daily inherits runtime and model", (route["runtime"], route["model"]) == ("claude", "opus"))
    check("daily uses medium effort", route["effort"] == "medium")
    route = routing.resolve(config, "review", session=codex)
    check("review defaults opposite", (route["runtime"], route["model"], route["effort"]) == ("claude", "fable", "high"))
    route = routing.resolve(config, "review", session=codex, explicit_runtime="codex")
    check("explicit review runtime uses its role default", (route["runtime"], route["model"]) == ("codex", "gpt-5.6-sol"))
    route = routing.resolve(config, "review", session=codex, same_model=True, explicit_effort="xhigh")
    check("same-model review inherits with effort override", (route["runtime"], route["model"], route["effort"]) == ("codex", "gpt-5.6-sol", "xhigh"))
    route = routing.resolve(config, "protected-research", session=claude)
    check("protected research from Claude uses Codex default", (route["runtime"], route["model"]) == ("codex", "gpt-5.6-sol"))
    route = routing.resolve(config, "protected-research", session=codex)
    check("protected research from Codex inherits", route["source"][0] == "session")
    route = routing.resolve(config, "unsafe-research", session=claude)
    check("unsafe research inherits full session", (route["runtime"], route["model"], route["effort"]) == ("claude", "opus", "high"))

    for name, call in (
        ("session-required roles fail closed", lambda: routing.resolve(config, "dispatch")),
        ("unknown model without runtime fails closed", lambda: routing.resolve(config, "dispatch", session=codex, explicit_model="unknown")),
        ("invalid effort fails closed", lambda: routing.resolve(config, "dispatch", session=codex, explicit_effort="ultra")),
    ):
        try:
            call()
        except routing.RoutingError:
            check(name, True)
        else:
            check(name, False)

    saved = routing.capture_session(config, "session-1", **codex, source="test")
    loaded = routing.load_session(config, "session-1")
    check("session snapshot round trip", loaded["config_sha256"] == saved["config_sha256"] and loaded["model"] == codex["model"])
    route = routing.resolve(config, "dispatch", session=loaded)
    check("session discovery source is preserved", route["source"][0] == "session:test")
    guessed = dict(codex, source="tracked-default")
    try:
        routing.resolve(config, "dispatch", session=guessed)
    except routing.RoutingError:
        check("guessed session default fails exact inheritance", True)
    else:
        check("guessed session default fails exact inheritance", False)
    check("native configs initially synchronized", routing.sync_native(config, apply=False) == [])
    path = root / ".codex/profiles/default.toml"
    path.write_text(path.read_text().replace('model = "gpt-5.6-sol"', 'model = "drift"'), encoding="utf-8")
    check("native drift detected", ".codex/profiles/default.toml" in routing.sync_native(config, apply=False))
    routing.sync_native(config, apply=True)
    check("native drift repaired", routing.sync_native(config, apply=False) == [])

    (root / "config/model-routing.local.toml").write_text(
        '[runtimes.claude]\nmodel = "sonnet"\n'
        '[roles.review.claude]\nmodel = "opus"\n'
        '[model_registry]\nsonnet = "claude"\n',
        encoding="utf-8",
    )
    local = routing.load_config(root)
    check("local runtime override is visible", local.local_override and local.runtime_default("claude")["model"] == "sonnet")
    check("local reviewer override is independent", local.reviewer_default("claude")["model"] == "opus")
    route = routing.resolve(local, "review", session=codex)
    check("review uses local role default", route["model"] == "opus")

    session_dir = root / ".codex/sessions/2026/07/18"
    session_dir.mkdir(parents=True)
    thread_id = "019f72c4-816e-7200-a399-505adaa350e0"
    record = session_dir / f"rollout-{thread_id}.jsonl"
    record.write_text(json.dumps({"type": "turn_context", "payload": {"model": "current-codex", "effort": "xhigh"}}) + "\n", encoding="utf-8")
    old_home = os.environ.get("HOME")
    os.environ["HOME"] = str(root)
    try:
        detected = routing.codex_session_route(thread_id)
    finally:
        if old_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = old_home
    check("Codex current session route discovered", detected == {"runtime": "codex", "model": "current-codex", "effort": "xhigh"})

print("model routing tests passed")
