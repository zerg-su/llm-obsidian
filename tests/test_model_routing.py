#!/usr/bin/env python3
"""Hermetic contract tests for session-aware model routing."""

from __future__ import annotations

import copy
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

    def reject_invalid_config(
        label: str, expected: str, mutate: object
    ) -> None:
        candidate = copy.deepcopy(config.data)
        mutate(candidate)
        try:
            routing._validate(candidate)
        except routing.RoutingError as exc:
            check(label, expected in str(exc))
        else:
            check(label, False)

    validation_cases = (
        (
            "routing schema invariant fails closed",
            "schema_version must be 1",
            lambda value: value.__setitem__("schema_version", 2),
        ),
        (
            "runtime table invariant fails closed",
            "define exactly codex and claude runtimes",
            lambda value: value["runtimes"].pop("claude"),
        ),
        (
            "registry presence invariant fails closed",
            "model_registry must be a non-empty table",
            lambda value: value["model_registry"].clear(),
        ),
        (
            "runtime default registration invariant fails closed",
            "default model 'unregistered' is not registered for codex",
            lambda value: value["runtimes"]["codex"].__setitem__(
                "model", "unregistered"
            ),
        ),
        (
            "role table invariant fails closed",
            "roles must define exactly",
            lambda value: value["roles"].pop("review"),
        ),
        (
            "role effort invariant fails closed",
            "claude effort must be one of",
            lambda value: value["roles"]["daily"].__setitem__(
                "effort", "ultra"
            ),
        ),
        (
            "review role registration invariant fails closed",
            "review model 'fable' is not registered for codex",
            lambda value: value["roles"]["review"]["codex"].__setitem__(
                "model", "fable"
            ),
        ),
        (
            "registry entry invariant fails closed",
            "model_registry entries must map non-empty model names",
            lambda value: value["model_registry"].__setitem__("", "codex"),
        ),
        (
            "release alias set invariant fails closed",
            "model_aliases must define exactly four release aliases",
            lambda value: value["model_aliases"].pop("sol"),
        ),
        (
            "diagnostic model invariant fails closed",
            "diagnostic-fast.model must be a registered model or alias",
            lambda value: value["roles"]["diagnostic-fast"].__setitem__(
                "model", "unregistered"
            ),
        ),
        (
            "review profile runtime invariant fails closed",
            "review profile alias/runtime mismatch: simple.codex",
            lambda value: value["review_profiles"]["simple"][
                "codex"
            ].__setitem__("model", "opus"),
        ),
        (
            "legacy review default invariant fails closed",
            "legacy review model 'fable' is not registered for codex",
            lambda value: value["legacy_review_defaults"][
                "codex"
            ].__setitem__("model", "fable"),
        ),
    )
    for validation_case in validation_cases:
        reject_invalid_config(*validation_case)

    codex = {"runtime": "codex", "model": "gpt-5.6-sol", "effort": "high"}
    claude = {
        "runtime": "claude",
        "model": "claude-opus-5",
        "effort": "high",
    }

    check(
        "Claude runtime default is Opus 5",
        config.runtime_default("claude")["model"] == "claude-opus-5",
    )
    check(
        "Claude simple reviewer default is Opus 5",
        config.reviewer_default("claude")["model"] == "claude-opus-5",
    )
    check("Claude deep reviewer default is Fable", config.reviewer_default("claude", "deep")["model"] == "fable")
    check("historical reviewer defaults are centrally tracked", config.legacy_reviewer_default("claude") == {
        "runtime": "claude", "model": "fable", "effort": "high"
    })
    check(
        "all concrete defaults are discoverable",
        config.default_models()
        == {"gpt-5.6-sol", "gpt-5.6-terra", "claude-opus-5", "fable"},
    )
    check("Sol alias resolves to concrete Codex target", config.resolve_alias("sol") == {"runtime": "codex", "model": "gpt-5.6-sol"})

    route = routing.resolve(config, "dispatch", session=codex)
    check("dispatch inherits exact session", (route["runtime"], route["model"], route["effort"]) == ("codex", "gpt-5.6-sol", "high"))
    route = routing.resolve(config, "daily", session=claude)
    check(
        "daily inherits runtime and model",
        (route["runtime"], route["model"])
        == ("claude", "claude-opus-5"),
    )
    check("daily uses medium effort", route["effort"] == "medium")
    route = routing.resolve(config, "review", session=codex)
    check(
        "simple review defaults opposite",
        (route["runtime"], route["model"], route["effort"])
        == ("claude", "claude-opus-5", "high"),
    )
    route = routing.resolve(config, "review", session=codex, review_profile="deep")
    check(
        "deep review uses the opposite runtime deep profile",
        (route["runtime"], route["model"], route["effort"])
        == ("claude", "fable", "xhigh"),
    )
    route = routing.resolve(config, "review", session=codex, explicit_runtime="codex")
    check("explicit review runtime uses its role default", (route["runtime"], route["model"]) == ("codex", "gpt-5.6-sol"))
    route = routing.resolve(config, "review", session=codex, same_model=True, explicit_effort="xhigh")
    check("same-model review inherits with effort override", (route["runtime"], route["model"], route["effort"]) == ("codex", "gpt-5.6-sol", "xhigh"))
    route = routing.resolve(
        config, "review", session=claude, same_model=True, review_profile="deep"
    )
    check(
        "same-runtime deep review still uses its deep profile",
        (route["runtime"], route["model"], route["effort"])
        == ("claude", "fable", "xhigh"),
    )
    route = routing.resolve(
        config, "review", session=claude, explicit_model="terra"
    )
    check(
        "review model alias selects its registered runtime",
        (route["runtime"], route["model"]) == ("codex", "gpt-5.6-terra"),
    )
    route = routing.resolve(config, "dispatch", session=codex, explicit_model="sonnet")
    check("registered Sonnet override infers Claude runtime", (route["runtime"], route["model"]) == ("claude", "sonnet"))
    route = routing.resolve(config, "dispatch", session=claude, explicit_model="gpt-5.6-terra")
    check("registered Terra override infers Codex runtime", (route["runtime"], route["model"]) == ("codex", "gpt-5.6-terra"))
    route = routing.resolve(config, "protected-research", session=claude)
    check("protected research from Claude uses Codex default", (route["runtime"], route["model"]) == ("codex", "gpt-5.6-sol"))
    route = routing.resolve(config, "protected-research", session=codex)
    check("protected research from Codex inherits", route["source"][0] == "session")
    route = routing.resolve(config, "diagnostic-fast", session=claude)
    check(
        "fast diagnostics use bounded Terra low without inheriting session context",
        (route["runtime"], route["model"], route["effort"])
        == ("codex", "gpt-5.6-terra", "low")
        and route["source"] == ["diagnostic-fast-profile"],
    )
    route = routing.resolve(config, "unsafe-research", session=claude)
    check(
        "unsafe research inherits full session",
        (route["runtime"], route["model"], route["effort"])
        == ("claude", "claude-opus-5", "high"),
    )
    route = routing.resolve(config, "dispatch", session=claude, explicit_model="terra")
    check("registered alias override infers runtime", (route["runtime"], route["model"]) == ("codex", "gpt-5.6-terra"))

    for name, call in (
        ("session-required roles fail closed", lambda: routing.resolve(config, "dispatch")),
        ("unknown model without runtime fails closed", lambda: routing.resolve(config, "dispatch", session=codex, explicit_model="unknown")),
        ("review concrete model override fails closed", lambda: routing.resolve(config, "review", session=codex, explicit_runtime="codex", explicit_model="gpt-5.6-sol")),
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

    tracked_path = root / "config/model-routing.toml"
    tracked = tracked_path.read_text(encoding="utf-8")
    wrong_generation = tracked.replace(
        'target = "claude-opus-5"\nexpected_generation = 5',
        'target = "claude-opus-4-8"\nexpected_generation = 5',
        1,
    ).replace(
        '"claude-opus-5" = "claude"',
        '"claude-opus-5" = "claude"\n"claude-opus-4-8" = "claude"',
        1,
    )
    tracked_path.write_text(wrong_generation, encoding="utf-8")
    try:
        routing.load_config(root)
    except routing.RoutingError as exc:
        check(
            "versioned Claude alias generation drift fails closed",
            "expected generation drift" in str(exc),
        )
    else:
        check("versioned Claude alias generation drift fails closed", False)
    tracked_path.write_text(tracked, encoding="utf-8")

    (root / "config/model-routing.local.toml").write_text(
        '[runtimes.claude]\nmodel = "sonnet"\n'
        '[model_aliases.sol]\ntarget = "codex-review"\n'
        '[model_registry]\nsonnet = "claude"\ncodex-review = "codex"\n',
        encoding="utf-8",
    )
    local = routing.load_config(root)
    check("local runtime override is visible", local.local_override and local.runtime_default("claude")["model"] == "sonnet")
    check("local reviewer override is independent", local.reviewer_default("codex")["model"] == "codex-review")
    route = routing.resolve(local, "review", session=codex)
    check("review uses local role default", route["model"] == "claude-opus-5")
    reviewer_profile = local.root / ".codex/profiles/reviewer-readonly.toml"
    check(
        "native reviewer profile uses reviewer role default",
        routing.native_targets(local)[reviewer_profile]["model"] == "codex-review",
    )

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
