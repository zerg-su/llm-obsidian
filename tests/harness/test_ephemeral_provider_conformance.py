#!/usr/bin/env python3
"""Equal hermetic conformance for Claude print and Codex exec adapters."""

from __future__ import annotations

import dataclasses
import json
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from harness.adapters.claude import ClaudeEphemeralAdapter  # noqa: E402
from harness.adapters.codex import CodexEphemeralAdapter  # noqa: E402
from harness.ephemeral_provider import (  # noqa: E402
    EphemeralProcessResult,
    EphemeralProviderError,
    EphemeralRunSpec,
    EphemeralTransportRegistry,
)
from harness.runtime_provider import default_ephemeral_transport_registry  # noqa: E402


def check(label: str, value: bool) -> None:
    if not value:
        raise AssertionError(label)
    print(f"OK   {label}")


def rejected(label: str, action) -> None:
    try:
        action()
    except EphemeralProviderError:
        check(label, True)
    else:
        check(label, False)


with tempfile.TemporaryDirectory(prefix="ephemeral-provider.") as raw:
    scratch = Path(raw).resolve()
    context = scratch / "context.json"
    schema_path = scratch / "output-schema.json"
    result_path = scratch / "result.json"
    runtime_home = scratch / "runtime-home"
    runtime_home.mkdir(mode=0o700)
    context.write_text(
        json.dumps({"question": "Return one typed verdict."}) + "\n",
        encoding="utf-8",
    )
    schema = {
        "type": "object",
        "properties": {
            "verdict": {"type": "string", "enum": ["approved", "blocked"]},
            "score": {"type": "integer"},
        },
        "required": ["verdict", "score"],
        "additionalProperties": False,
    }
    schema_path.write_text(
        json.dumps(schema, sort_keys=True) + "\n", encoding="utf-8"
    )

    def spec(provider: str) -> EphemeralRunSpec:
        return EphemeralRunSpec(
            logical_provider=provider,
            model="bounded-model",
            effort="xhigh",
            context_packet=context,
            output_schema=schema_path,
            result_path=result_path,
            runtime_home=runtime_home,
            cwd=scratch,
            capabilities=("read-context", "schema-output"),
            auth_profile=(
                "native-subscription" if provider == "anthropic" else "chatgpt"
            ),
            turn_budget=1,
            wall_clock_deadline=120.0,
            operation_id="review-operation",
            run_id="review-run",
            generation=1,
            effect_id="review-input",
        )

    adapters = {
        "anthropic": ClaudeEphemeralAdapter(Path("/opt/bin/claude")),
        "openai": CodexEphemeralAdapter(Path("/opt/bin/codex")),
    }
    registry = EphemeralTransportRegistry(adapters)
    ambient = {
        "HOME": str(scratch / "home"),
        "PATH": "/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "TERM": "xterm-256color",
        "ANTHROPIC_API_KEY": "secret-anthropic",
        "ANTHROPIC_AUTH_TOKEN": "secret-token",
        "OPENAI_API_KEY": "secret-openai",
        "CODEX_API_KEY": "secret-codex",
        "AWS_SECRET_ACCESS_KEY": "secret-cloud",
        "RANDOM_SECRET": "secret-random",
    }

    commands = {
        provider: registry.compile(spec(provider), env=ambient)
        for provider in adapters
    }
    auth_commands = {
        provider: registry.auth_command(spec(provider), env=ambient)
        for provider in adapters
    }
    check(
        "auth probes are fixed no-model-effect commands in sanitized environments",
        auth_commands["anthropic"].argv == ("/opt/bin/claude", "auth", "status")
        and auth_commands["openai"].argv == ("/opt/bin/codex", "login", "status")
        and all(not command.model_effect for command in auth_commands.values())
        and all(
            "secret-" not in value
            for command in auth_commands.values()
            for value in command.environment.values()
        ),
    )
    claude = commands["anthropic"]
    codex = commands["openai"]
    check(
        "Claude print command is fixed, bounded, schema-only, and non-persistent",
        claude.argv[0] == "/opt/bin/claude"
        and "--print" in claude.argv
        and "--json-schema" in claude.argv
        and "--no-session-persistence" in claude.argv
        and claude.argv[claude.argv.index("--max-turns") + 1] == "1"
        and claude.argv[claude.argv.index("--tools") + 1] == ""
        and claude.stdin == context.read_bytes(),
    )
    check(
        "Codex exec command is fixed, ephemeral, schema-only, and read-only",
        codex.argv[0] == "/opt/bin/codex"
        and "exec" in codex.argv
        and "--ephemeral" in codex.argv
        and "--json" in codex.argv
        and "--output-schema" in codex.argv
        and codex.argv[codex.argv.index("--sandbox") + 1] == "read-only"
        and codex.argv[codex.argv.index("--ask-for-approval") + 1] == "never"
        and codex.stdin == context.read_bytes(),
    )
    check(
        "both child environments remove ambient credentials and arbitrary secrets",
        all(
            "secret-" not in value
            and not {
                "ANTHROPIC_API_KEY",
                "ANTHROPIC_AUTH_TOKEN",
                "OPENAI_API_KEY",
                "CODEX_API_KEY",
                "AWS_SECRET_ACCESS_KEY",
                "RANDOM_SECRET",
            }
            & set(command.environment)
            for command in commands.values()
            for value in command.environment.values()
        )
        and commands["openai"].environment.get("CODEX_HOME")
        == str(runtime_home)
        and "CODEX_HOME" not in commands["anthropic"].environment,
    )
    default_registry = default_ephemeral_transport_registry(
        claude_binary=Path("/opt/bin/claude"),
        codex_binary=Path("/opt/bin/codex"),
    )
    check(
        "runtime provider publishes the replaceable default logical registry",
        default_registry.compile(spec("anthropic"), env=ambient).transport
        == "claude-print"
        and default_registry.compile(spec("openai"), env=ambient).transport
        == "codex-exec",
    )
    check(
        "provider-neutral spec contains logical routes but no CLI transport names",
        "claude-print" not in repr(dataclasses.asdict(spec("anthropic")))
        and "codex-exec" not in repr(dataclasses.asdict(spec("openai"))),
    )

    claude_ready = registry.preflight(
        spec("anthropic"),
        stdout=json.dumps(
            {
                "loggedIn": True,
                "authMethod": "claude.ai",
                "apiProvider": "firstParty",
                "subscriptionType": "max",
            }
        ),
        stderr="",
        returncode=0,
    )
    codex_ready = registry.preflight(
        spec("openai"),
        stdout="Logged in using ChatGPT",
        stderr="",
        returncode=0,
    )
    check(
        "native Claude subscription and Codex ChatGPT premises are equally ready",
        claude_ready.status == codex_ready.status == "ready"
        and claude_ready.model_effect_allowed
        and codex_ready.model_effect_allowed,
    )
    for provider, stdout in (
        (
            "anthropic",
            json.dumps(
                {
                    "loggedIn": True,
                    "authMethod": "apiKey",
                    "apiProvider": "firstParty",
                }
            ),
        ),
        ("openai", "Logged in using an API key"),
    ):
        blocked = registry.preflight(
            spec(provider), stdout=stdout, stderr="", returncode=0
        )
        probe = registry.bounded_probe(
            spec(provider), blocked, env=ambient
        )
        check(
            f"{provider} ambiguous billing path stops before a model effect",
            blocked.status == "billing-profile-unverified"
            and not blocked.model_effect_allowed
            and probe.command is None
            and probe.max_model_effects == 1,
        )

    probes = {
        "anthropic": registry.bounded_probe(
            spec("anthropic"), claude_ready, env=ambient
        ),
        "openai": registry.bounded_probe(
            spec("openai"), codex_ready, env=ambient
        ),
    }
    check(
        "bounded native-account probes permit exactly one compiled model effect",
        all(
            probe.command is not None and probe.max_model_effects == 1
            for probe in probes.values()
        ),
    )

    typed_output = {"verdict": "approved", "score": 7}
    observations = {
        "anthropic": EphemeralProcessResult(
            provider_session_id="claude-session",
            process_identity="a" * 64,
            source_id="claude-process-1",
            stdout=json.dumps(
                {
                    "type": "result",
                    "subtype": "success",
                    "is_error": False,
                    "structured_output": typed_output,
                }
            ).encode(),
            stderr=b"",
            result_bytes=b"",
            returncode=0,
        ),
        "openai": EphemeralProcessResult(
            provider_session_id="codex-session",
            process_identity="b" * 64,
            source_id="codex-process-1",
            stdout=(
                json.dumps({"type": "thread.started"})
                + "\n"
                + json.dumps({"type": "turn.completed"})
                + "\n"
            ).encode(),
            stderr=b"",
            result_bytes=json.dumps(typed_output).encode(),
            returncode=0,
        ),
    }
    success = {
        provider: registry.normalize(spec(provider), observations[provider])
        for provider in adapters
    }
    expected_kinds = (
        "provider-started",
        "input-accepted",
        "result-published",
        "process-exited",
        "resource-closed",
    )
    check(
        "both providers normalize success through one event/result contract",
        all(
            result.disposition == "succeeded"
            and result.result == typed_output
            and tuple(item.kind for item in result.events) == expected_kinds
            and all(item.kind != "turn-stopped" for item in result.events)
            for result in success.values()
        ),
    )
    check(
        "transport identity remains diagnostic and never changes workflow identity",
        success["anthropic"].transport == "claude-print"
        and success["openai"].transport == "codex-exec"
        and success["anthropic"].events[0].identity.operation_id
        == success["openai"].events[0].identity.operation_id
        == "review-operation",
    )

    malformed = {
        "anthropic": dataclasses.replace(
            observations["anthropic"], stdout=b'{"type":"result"'
        ),
        "openai": dataclasses.replace(
            observations["openai"], stdout=b'{"type":"thread.started"\n'
        ),
    }
    invalid = {
        provider: registry.normalize(spec(provider), malformed[provider])
        for provider in adapters
    }
    check(
        "truncated provider output becomes a typed event-gap and durable close",
        all(
            result.disposition == "schema-invalid"
            and "event-gap" in tuple(item.kind for item in result.events)
            and result.events[-1].kind == "resource-closed"
            for result in invalid.values()
        ),
    )

    nonzero = {
        provider: registry.normalize(
            spec(provider),
            dataclasses.replace(
                observations[provider],
                returncode=1,
                stderr=b"usage limit exhausted secret-provider-detail",
            ),
        )
        for provider in adapters
    }
    check(
        "nonzero exits are typed equally and never retain provider stderr",
        all(
            result.disposition == "usage-exhausted"
            and result.events[-1].kind == "resource-closed"
            and "secret-provider-detail" not in repr(result)
            for result in nonzero.values()
        ),
    )

    rejected(
        "registry cannot silently substitute a different logical provider",
        lambda: EphemeralTransportRegistry(
            {"anthropic": CodexEphemeralAdapter(Path("/opt/bin/codex"))}
        ),
    )
    rejected(
        "unregistered provider cannot fall back to an interactive transport",
        lambda: registry.compile(
            dataclasses.replace(spec("openai"), logical_provider="local"),
            env=ambient,
        ),
    )

print("ephemeral provider conformance matrix: ok")
