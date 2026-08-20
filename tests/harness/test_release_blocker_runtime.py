#!/usr/bin/env python3
"""Release-blocker coverage for the restartable provider harness."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from harness import capabilities
from harness import runtime_worker
from harness.adapters.claude import ClaudeDriver, ClaudeDriverError
from harness.adapters.codex import CodexDriver, CodexDriverError
from harness.contracts import AttentionReason, OperationSpec, RuntimeRoute
from harness.prompts import classify
from harness.runtime_worker import automate_prompt
from harness.store import OperationStore
from harness.supervisor import OperationSupervisor, SupervisorError


def check(label: str, value: bool) -> None:
    if not value:
        raise AssertionError(label)
    print(f"OK   {label}")


FINGERPRINT = "a" * 64
route = RuntimeRoute("claude", "opus-5", "high", "executor", FINGERPRINT)

claude_driver = ClaudeDriver(
    Path("/usr/bin/claude"), registered_models=frozenset({"opus-5"})
)
codex_driver = CodexDriver(
    Path("/usr/bin/codex"), registered_models=frozenset({"gpt-5.6-sol"})
)
check(
    "provider adapters accept registered model effort and fixed profile",
    "--permission-mode" in claude_driver.command(route)
    and "read-only"
    in codex_driver.command(
        RuntimeRoute(
            "codex",
            "gpt-5.6-sol",
            "xhigh",
            "research-unsafe",
            FINGERPRINT,
        )
    ),
)
codex_research_command = codex_driver.command(
    RuntimeRoute(
        "codex",
        "gpt-5.6-sol",
        "xhigh",
        "research-unsafe",
        FINGERPRINT,
    )
)
check(
    "Codex pins resumed sessions to the current task worktree",
    "tui.resume_cwd=\"current\"" in codex_research_command,
)
for label, call in (
    (
        "Claude rejects an unregistered model",
        lambda: claude_driver.command(
            RuntimeRoute("claude", "unknown-model", "high", "executor", FINGERPRINT)
        ),
    ),
    (
        "Codex rejects an unsupported effort",
        lambda: codex_driver.command(
            RuntimeRoute(
                "codex", "gpt-5.6-sol", "turbo", "executor", FINGERPRINT
            )
        ),
    ),
    (
        "Claude resume requires an exact checkpoint",
        lambda: claude_driver.resume_command(route, ""),
    ),
):
    try:
        call()
    except (ClaudeDriverError, CodexDriverError):
        check(label, True)
    else:
        check(label, False)
check(
    "unknown provider status output fails closed",
    not claude_driver.authenticated_subscription("future output", "", 0)
    and not codex_driver.authenticated_subscription("future output", "", 0),
)
check(
    "provider restart resumes the exact Claude or Codex session",
    runtime_worker.provider_resume_argv(
        ("claude", "--model", "opus-5", "--", "continue"),
        "claude",
        "session-1",
    )
    == (
        "claude",
        "--model",
        "opus-5",
        "--resume",
        "session-1",
        "--",
    )
    and runtime_worker.provider_resume_argv(
        (
            "codex",
            "--model",
            "gpt-5.6-sol",
            "--config",
            'tui.resume_cwd="current"',
        ),
        "codex",
        "thread-1",
        deferred_initial_input=True,
    )
    == (
        "codex",
        "--model",
        "gpt-5.6-sol",
        "--config",
        'tui.resume_cwd="current"',
        "resume",
        "thread-1",
    ),
)
check(
    "provider restart never re-injects the original task prompt",
    "continue"
    not in runtime_worker.provider_resume_argv(
        ("claude", "--model", "opus-5", "--", "continue"),
        "claude",
        "session-1",
    )
    and "continue"
    not in runtime_worker.provider_resume_argv(
        ("codex", "--model", "gpt-5.6-sol", "continue"),
        "codex",
        "thread-1",
        deferred_initial_input=False,
    ),
)
try:
    runtime_worker.provider_resume_argv(
        ("codex", "continue"), "codex", ""
    )
except runtime_worker.RuntimeWorkerError as exc:
    check("provider restart fails closed without a checkpoint", "checkpoint" in str(exc))
else:
    check("provider restart fails closed without a checkpoint", False)

with tempfile.TemporaryDirectory(prefix="runtime-worker-early-failure.") as raw:
    failure_root = Path(raw)
    launch_path = failure_root / "launch.json"
    ready_path = failure_root / "ready.json"
    launch_path.write_text(
        json.dumps({"ready_path": str(ready_path)}), encoding="utf-8"
    )
    os.chmod(failure_root, 0o700)
    original_run = runtime_worker.run
    runtime_worker.run = lambda _path: (_ for _ in ()).throw(
        runtime_worker.RuntimeWorkerError("runtime launch authority drifted")
    )
    try:
        status = runtime_worker.main(["--spec", str(launch_path)])
    finally:
        runtime_worker.run = original_run
    startup_failure = json.loads(ready_path.read_text(encoding="utf-8"))
    check(
        "early runtime-worker failure leaves one durable diagnostic",
        status == 2
        and startup_failure
        == {
            "schema_version": 1,
            "status": "failed",
            "reason": "runtime launch authority drifted",
        },
    )


class CallbackWakeCmux:
    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []

    def send(self, surface_id: str, value: str) -> None:
        self.events.append((surface_id, value))

    def send_key(self, surface_id: str, value: str) -> None:
        self.events.append((surface_id, value))


with tempfile.TemporaryDirectory(prefix="callback-wake.") as raw:
    wake_cmux = CallbackWakeCmux()
    wake_spec = {
        "origin_surface": "11111111-1111-4111-8111-111111111111",
        "callback_wake": "Run the exact idempotent current-review drive.",
    }
    first_wake = runtime_worker.publish_callback_wake(
        wake_spec, Path(raw), "callback-wake-1", wake_cmux
    )
    repeated_wake = runtime_worker.publish_callback_wake(
        wake_spec, Path(raw), "callback-wake-1", wake_cmux
    )
    marker = json.loads(
        (Path(raw) / "callback-wake.json").read_text(encoding="utf-8")
    )
    check(
        "accepted current-review callback wakes its exact coordinator once",
        first_wake
        and repeated_wake
        and wake_cmux.events
        == [
            (
                "11111111-1111-4111-8111-111111111111",
                "Run the exact idempotent current-review drive.",
            ),
            ("11111111-1111-4111-8111-111111111111", "Enter"),
        ]
        and marker["status"] == "sent",
    )


class PartialCallbackWakeCmux(CallbackWakeCmux):
    def __init__(self, *, fail_after_key: bool = False) -> None:
        super().__init__()
        self.fail_after_key = fail_after_key

    def send_key(self, surface_id: str, value: str) -> None:
        if self.fail_after_key:
            self.events.append((surface_id, value))
        raise RuntimeError("callback wake kill point")


for label, fail_after_key, expected_events in (
    (
        "after-send",
        False,
        [
            (
                "11111111-1111-4111-8111-111111111111",
                "Run the exact idempotent current-review drive.",
            )
        ],
    ),
    (
        "after-enter",
        True,
        [
            (
                "11111111-1111-4111-8111-111111111111",
                "Run the exact idempotent current-review drive.",
            ),
            ("11111111-1111-4111-8111-111111111111", "Enter"),
        ],
    ),
):
    with tempfile.TemporaryDirectory(prefix=f"callback-wake-{label}.") as raw:
        wake_cmux = PartialCallbackWakeCmux(fail_after_key=fail_after_key)
        wake_spec = {
            "origin_surface": "11111111-1111-4111-8111-111111111111",
            "callback_wake": "Run the exact idempotent current-review drive.",
        }
        first_wake = runtime_worker.publish_callback_wake(
            wake_spec, Path(raw), "callback-wake-1", wake_cmux
        )
        repeated_wake = runtime_worker.publish_callback_wake(
            wake_spec, Path(raw), "callback-wake-1", wake_cmux
        )
        marker = json.loads(
            (Path(raw) / "callback-wake.json").read_text(encoding="utf-8")
        )
        check(
            f"callback wake {label} crash is fail-closed and never replayed",
            not first_wake
            and not repeated_wake
            and wake_cmux.events == expected_events
            and marker["status"] == "effect-uncertain",
        )


class ConcurrentCallbackWakeCmux(CallbackWakeCmux):
    def __init__(self) -> None:
        super().__init__()
        self.first_send = threading.Event()
        self.release_send = threading.Event()

    def send(self, surface_id: str, value: str) -> None:
        self.events.append((surface_id, value))
        self.first_send.set()
        if not self.release_send.wait(timeout=2):
            raise RuntimeError("concurrent wake test timed out")


with tempfile.TemporaryDirectory(prefix="callback-wake-concurrent.") as raw:
    wake_cmux = ConcurrentCallbackWakeCmux()
    wake_spec = {
        "origin_surface": "11111111-1111-4111-8111-111111111111",
        "callback_wake": "Run the exact idempotent current-review drive.",
    }
    results: list[bool] = []
    errors: list[BaseException] = []

    def publish() -> None:
        try:
            results.append(
                runtime_worker.publish_callback_wake(
                    wake_spec, Path(raw), "callback-wake-1", wake_cmux
                )
            )
        except BaseException as exc:  # noqa: BLE001 - test retains thread failure
            errors.append(exc)

    first = threading.Thread(target=publish)
    second = threading.Thread(target=publish)
    first.start()
    assert wake_cmux.first_send.wait(timeout=1)
    second.start()
    wake_cmux.release_send.set()
    first.join(timeout=3)
    second.join(timeout=3)
    check(
        "concurrent callback wake reconcile has one provider-facing effect",
        not errors
        and not first.is_alive()
        and not second.is_alive()
        and results == [True, True]
        and wake_cmux.events
        == [
            (
                "11111111-1111-4111-8111-111111111111",
                "Run the exact idempotent current-review drive.",
            ),
            ("11111111-1111-4111-8111-111111111111", "Enter"),
        ],
    )
check(
    "durable harness state is repository-ignored",
    ".vault-meta/harness/" in (ROOT / ".gitignore").read_text(encoding="utf-8"),
)
exit_is_final = getattr(runtime_worker, "provider_exit_is_final", None)
check(
    "callback transports exit only after handling or durable shutdown",
    exit_is_final is not None
    and not exit_is_final(
        provider_exited=True,
        callback_mode="task-summary",
        callback_handled=False,
        operation_state="awaiting-callback",
        operation_profile="executor",
        callback_deadline_at=0.0,
    )
    and exit_is_final(
        provider_exited=True,
        callback_mode="task-summary",
        callback_handled=False,
        operation_state="exiting",
        operation_profile="executor",
        callback_deadline_at=0.0,
    )
    and not exit_is_final(
        provider_exited=True,
        callback_mode="envelope",
        callback_handled=False,
        operation_state="awaiting-callback",
        operation_profile="reviewer-callback",
        callback_deadline_at=1.0,
    )
    and exit_is_final(
        provider_exited=True,
        callback_mode="envelope",
        callback_handled=False,
        operation_state="attention-required",
        operation_profile="reviewer-callback",
        callback_deadline_at=1.0,
    ),
)


def successful_probe(
    command: list[str], **_kwargs: object
) -> subprocess.CompletedProcess[str]:
    cmux_help = {
        ("new-split", "--help"): "--surface --focus",
        ("workspace", "create", "--help"):
            "create [flags]\nclose <workspace>\n--window",
        ("new-workspace", "--help"): "--window --focus --cwd",
        ("workspace", "close", "--help"):
            "close <workspace>\n--window",
        ("identify", "--help"): "--surface",
        ("surface", "resume", "--help"):
            "resume get\nresume set\nresume show\nresume clear\n--surface\n--json",
        ("close-surface", "--help"): "--surface",
    }
    tail = tuple(command[1:])
    if tail in cmux_help:
        return subprocess.CompletedProcess(command, 0, cmux_help[tail], "")
    if tail == ("--help",):
        if Path(command[0]).name == "codex":
            return subprocess.CompletedProcess(
                command,
                0,
                "--model --config --sandbox --ask-for-approval",
                "",
            )
        return subprocess.CompletedProcess(
            command,
            0,
            "--model --effort --permission-mode",
            "",
        )
    if tail == ("login", "status"):
        return subprocess.CompletedProcess(
            command,
            0,
            "",
            (
                "WARNING: proceeding, even though we could not create PATH "
                "aliases: Operation not permitted (os error 1)\n"
                "Logged in using ChatGPT\n"
            ),
        )
    if tail == ("auth", "status"):
        return subprocess.CompletedProcess(
            command,
            0,
            json.dumps(
                {
                    "loggedIn": True,
                    "authMethod": "claude.ai",
                    "apiProvider": "firstParty",
                    "subscriptionType": "max",
                }
            ),
            "",
        )
    return subprocess.CompletedProcess(command, 2, "", "unexpected probe")


with tempfile.TemporaryDirectory(prefix="harness-release-blockers.") as raw:
    root = Path(raw)
    callback_dir = root / "callbacks"
    callback_dir.mkdir()
    calls: list[list[str]] = []
    surface_id = "11111111-1111-1111-1111-111111111111"
    wrapper_root = root / "cmux-cli-shims" / surface_id
    wrapper_root.mkdir(parents=True)
    wrapper = wrapper_root / "claude"
    wrapper.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    wrapper.chmod(0o700)
    wrapper_env = {
        "CMUX_SURFACE_ID": surface_id,
        "CMUX_CLAUDE_WRAPPER_SHIM": str(wrapper),
        "CMUX_CLAUDE_WRAPPER_SHIM_ROOT": str(wrapper_root),
    }
    durable_argv = ("/usr/bin/claude", "--model", "opus")
    rewrite = getattr(runtime_worker, "provider_argv", None)
    check(
        "runtime worker selects only the exact surface provider wrapper",
        rewrite is not None
        and rewrite(
            {
                "runtime": "claude",
                "surface_id": surface_id,
                "argv": durable_argv,
            },
            env=wrapper_env,
        )
        == (str(wrapper.resolve()), "--model", "opus")
        and durable_argv == ("/usr/bin/claude", "--model", "opus"),
    )
    mismatched_env = dict(wrapper_env)
    mismatched_env["CMUX_SURFACE_ID"] = (
        "22222222-2222-2222-2222-222222222222"
    )
    check(
        "runtime worker rejects a wrapper from another surface",
        rewrite(
            {
                "runtime": "claude",
                "surface_id": surface_id,
                "argv": durable_argv,
            },
            env=mismatched_env,
        )
        == durable_argv,
    )
    wrapper.chmod(0o722)
    check(
        "runtime worker rejects a writable provider wrapper",
        rewrite(
            {
                "runtime": "claude",
                "surface_id": surface_id,
                "argv": durable_argv,
            },
            env=wrapper_env,
        )
        == durable_argv,
    )
    wrapper.chmod(0o700)

    def record_probe(
        command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return successful_probe(command, **kwargs)

    report = capabilities.check(
        route,
        callback_dir=callback_dir,
        expected_routing_sha256=FINGERPRINT,
        which=lambda name: f"/usr/bin/{name}",
        runner=record_probe,
    )
    expected = {
        "cmux:anchored-split",
        "cmux:anchored-workspace",
        "cmux:canonical-workspace-create",
        "cmux:canonical-workspace-close",
        "cmux:identify-status",
        "cmux:typed-resume",
        "cmux:exact-close",
        "provider:authenticated",
        "provider:subscription",
        "provider:model-effort-profile",
        "callback:writable",
        "callback:profile-writable",
        "routing:fingerprint",
    }
    check(
        "zero-effect handshake proves the complete runtime route",
        report.compatible and expected <= set(report.capabilities),
    )
    codex_report = capabilities.check(
        RuntimeRoute(
            "codex",
            "gpt-5.6-sol",
            "high",
            "executor",
            FINGERPRINT,
        ),
        callback_dir=callback_dir,
        expected_routing_sha256=FINGERPRINT,
        which=lambda name: f"/usr/bin/{name}",
        runner=successful_probe,
    )
    check(
        "Codex capability handshake accepts the exact stderr login marker and PATH warning",
        codex_report.compatible and expected <= set(codex_report.capabilities),
    )
    check(
        "capability probes are read-only",
        all(
            "--help" in command
            or command[-2:] == ["auth", "status"]
            for command in calls
        ),
    )
    mismatched = capabilities.check(
        route,
        callback_dir=callback_dir,
        expected_routing_sha256="b" * 64,
        which=lambda name: f"/usr/bin/{name}",
        runner=successful_probe,
    )
    check(
        "routing fingerprint mismatch fails before any effect",
        not mismatched.compatible
        and mismatched.reason == AttentionReason.CAPABILITY_MISMATCH,
    )
    readonly_callback = capabilities.check(
        RuntimeRoute(
            "claude",
            "opus-5",
            "high",
            "reviewer-readonly",
            FINGERPRINT,
        ),
        callback_dir=callback_dir,
        expected_routing_sha256=FINGERPRINT,
        which=lambda name: f"/usr/bin/{name}",
        runner=successful_probe,
    )
    check(
        "read-only provider profile cannot claim callback permission",
        not readonly_callback.compatible
        and readonly_callback.reason == AttentionReason.CAPABILITY_MISMATCH,
    )

    claude_workspace = (
        "Accessing workspace:\n"
        "Quick safety check: Is this a project you created or one you trust?\n"
        "1. Yes, I trust this folder\n2. No, exit\n"
        "Enter to confirm · Esc to cancel\n"
    )
    claude_wrapped_workspace = (
        "Accessing workspace:\n\n"
        "/Users/zak/Projects/worktrees/\n"
        "llm-obsidian-2-3-0/\n"
        "a-long-provider-owned-review-\n"
        "workspace-path-that-wraps-\n"
        "over-several-terminal-lines/\n\n"
        "Quick safety check: Is\n"
        "this a project you created\n"
        "or one you trust? For example,\n"
        "your own code, a well-known\n"
        "open source project, or work\n"
        "from your team. If not, take\n"
        "a moment to inspect the files\n"
        "before continuing.\n\n"
        "Claude Code will be able to\n"
        "read, edit, and execute files\n"
        "in this folder.\n\n"
        "❯ 1. Yes, I trust this\n"
        "folder\n"
        "  2. No, exit\n\n"
        "Enter to confirm · Esc\n"
        "to cancel\n"
    )
    codex_narrow_workspace = "\n".join(
        (
            "Do you trust the contents of this directory?",
            "1. Yes, continue",
            "2. No, quit",
            "Press enter to continue",
        )
    )
    codex_narrow_workspace = "\n".join(
        fragment
        for line in codex_narrow_workspace.splitlines()
        for fragment in (line[index : index + 2] for index in range(0, len(line), 2))
    )
    codex_clipped_workspace = (
        "Do you\ntrust the\ncontents\nof this\ndirectory\n? Working\nwith\n"
        "untrusted\ncontents\ncomes\nwith\nhigher\nrisk of\nprompt\ninjection\n.\n"
        "Trusting\nthe\ndirectory\nallows\nproject-l\nocal\nconfig,\nhooks,\n"
        "and exec\npolicies\nto load.\n"
        "1. Yes,\ncontin\nue\n2. No,\nquit\nPress ent\n"
    )
    claude_mcp = (
        "New MCP server found in this project: context7\n"
        "MCP servers may execute code or access system resources.\n"
        "1. Use this MCP server\n"
        "2. Use this and all future MCP servers in this project\n"
        "3. Continue without using this MCP server\n"
        "Enter to confirm\n"
    )
    claude_first_run = (
        "Choose the text style that looks best with your terminal\n"
        "1. Dark mode\n2. Light mode\nEnter to confirm\n"
    )
    claude_exit = (
        "Background work is running\n"
        "The following will stop when you exit:\n"
        "1. Exit anyway\n2. Move to background and exit\n3. Stay\n"
        "Enter to confirm\n"
    )
    claude_auto_mode_onboarding = (
        "Set up auto mode for your environment?\n"
        "1. Set it up\n2. Not now\n3. Don't show again\n"
        "Enter to confirm · Esc to cancel\n"
    )
    claude_auto_mode_wizard = (
        "How would you describe the code you work on with Claude?\n"
        "1. Personal / hobby projects\n"
        "2. Open source\n"
        "3. Work / enterprise (private repos, sensitive data)\n"
        "4. A mix of these\n"
        "Enter to confirm · Esc to cancel\n"
    )
    codex_rate_limit_switch = (
        "Approaching rate limits\n"
        "Switch to gpt-5.6-luna for lower credit usage?\n"
        "› 1. Switch to gpt-5.6-luna\n"
        "2. Keep current model\n"
        "3. Keep current model (never show again)\n"
        "Press enter to confirm or esc to go back\n"
    )
    check(
        "known production dialogs have exact deterministic key sequences",
        classify("claude", claude_workspace).keys == ("Enter",)
        and classify("claude", claude_wrapped_workspace).keys == ("Enter",)
        and classify("codex", codex_narrow_workspace).keys == ("Enter",)
        and classify("codex", codex_clipped_workspace).keys == ("Enter",)
        and classify("claude", claude_mcp).keys == ("Tab", "Tab", "Enter")
        and classify("claude", claude_first_run).keys == ("Enter",)
        and classify("claude", claude_exit, closure_armed=True).keys == ("Enter",)
        and classify("claude", claude_auto_mode_onboarding).keys
        == ("Esc",)
        and classify("claude", claude_auto_mode_wizard).keys == ("Esc",)
        and classify("codex", codex_rate_limit_switch).keys == ("down", "Enter"),
    )
    auto_mode_near_match = claude_auto_mode_onboarding.replace(
        "2. Not now", "2. Configure automatically", 1
    )
    check(
        "Claude auto-mode onboarding rejects a changed safe option",
        not classify("claude", auto_mode_near_match).recognized
        and not classify("claude", auto_mode_near_match).keys,
    )
    clipped_near_match = codex_clipped_workspace.replace(
        "trust the", "inspect the", 1
    )
    check(
        "clipped Codex trust footer rejects near-match",
        not classify("codex", clipped_near_match).recognized
        and not classify("codex", clipped_near_match).keys,
    )
    rate_limit_near_match = codex_rate_limit_switch.replace(
        "2. Keep current model", "2. Switch automatically", 1
    )
    check(
        "Codex rate-limit choice rejects changed safe option",
        not classify("codex", rate_limit_near_match).recognized
        and not classify("codex", rate_limit_near_match).keys,
    )
    unknown = (
        "A new provider decision appeared\n"
        "1. Enable experimental behavior\n2. Stop\nEnter to proceed\n"
    )
    check(
        "unknown native choice is interactive but never allowlisted",
        classify("claude", unknown).interactive
        and not classify("claude", unknown).recognized
        and not classify("claude", unknown).keys,
    )

    store = OperationStore(root / "store")
    spec = OperationSpec(
        "op-1",
        "key-1",
        "dispatch",
        "owner-1",
        route,
        "packet.json",
        "scoped",
    )
    store.create(spec, lane_id="lane-1", run_id="run-1")
    for state in ("preflight", "starting", "running"):
        store.transition("owner-1", "op-1", state)

    class FakeCmux:
        def __init__(self) -> None:
            self.keys: list[tuple[str, str]] = []

        def send_key(self, surface: str, key: str) -> None:
            self.keys.append((surface, key))

    auto_mode_dismissals: list[bool] = []
    for index in range(50):
        operation_id = f"op-auto-mode-{index}"
        auto_mode_spec = OperationSpec(
            operation_id,
            f"key-auto-mode-{index}",
            "dispatch",
            "owner-1",
            route,
            "packet.json",
            "scoped",
        )
        store.create(
            auto_mode_spec,
            lane_id="lane-1",
            run_id=f"run-auto-mode-{index}",
        )
        for state in ("preflight", "starting", "running"):
            store.transition("owner-1", operation_id, state)
        auto_mode_cmux = FakeCmux()
        automate_prompt(
            store,
            "owner-1",
            operation_id,
            route.runtime,
            "11111111-1111-1111-1111-111111111111",
            (
                claude_auto_mode_onboarding
                if index % 2 == 0
                else claude_auto_mode_wizard
            ),
            auto_mode_cmux,
        )
        auto_mode_dismissals.append(
            auto_mode_cmux.keys
            == [("11111111-1111-1111-1111-111111111111", "Esc")]
            and store.read("owner-1", operation_id).state == "running"
        )
    check(
        "Claude auto-mode setup dismissal stays model-free across 50 transitions",
        all(auto_mode_dismissals),
    )

    fake_cmux = FakeCmux()
    automate_prompt(
        store,
        "owner-1",
        "op-1",
        route.runtime,
        "11111111-1111-1111-1111-111111111111",
        unknown,
        fake_cmux,
    )
    attention = store.read("owner-1", "op-1")
    check(
        "unknown interactive prompt durably requests attention without input",
        attention.state == "attention-required"
        and attention.attention_reason == AttentionReason.PROMPT_UNKNOWN
        and fake_cmux.keys == [],
    )

    codex_route = RuntimeRoute(
        "codex", "gpt-5.6-sol", "xhigh", "executor", FINGERPRINT
    )
    rate_limit_spec = OperationSpec(
        "op-rate-limit",
        "key-rate-limit",
        "dispatch",
        "owner-1",
        codex_route,
        "packet.json",
        "scoped",
    )
    store.create(rate_limit_spec, lane_id="lane-1", run_id="run-rate-limit")
    for state in ("preflight", "starting", "running"):
        store.transition("owner-1", "op-rate-limit", state)
    rate_limit_cmux = FakeCmux()
    automate_prompt(
        store,
        "owner-1",
        "op-rate-limit",
        codex_route.runtime,
        "11111111-1111-1111-1111-111111111111",
        codex_rate_limit_switch,
        rate_limit_cmux,
    )
    check(
        "Codex rate-limit prompt keeps the bound model without disabling reminders",
        rate_limit_cmux.keys
        == [
            ("11111111-1111-1111-1111-111111111111", "down"),
            ("11111111-1111-1111-1111-111111111111", "Enter"),
        ]
        and store.read("owner-1", "op-rate-limit").state == "running",
    )

    budget_spec = OperationSpec(
        "op-budget",
        "key-budget",
        "dispatch",
        "owner-1",
        route,
        "packet.json",
        "scoped",
    )
    store.create(budget_spec, lane_id="lane-1", run_id="run-budget")
    budget = OperationSupervisor(store, "owner-1", "op-budget")
    budget.configure_budget(
        attempt_limit=2,
        model_restart_limit=1,
        time_budget_seconds=60,
        token_limit=100,
        now=1000.0,
    )
    budget.consume_attempt(tokens=40, now=1001.0)
    restarted = OperationSupervisor(
        OperationStore(root / "store"), "owner-1", "op-budget"
    )
    restarted.consume_attempt(tokens=50, now=1002.0)
    try:
        restarted.consume_attempt(tokens=1, now=1003.0)
    except SupervisorError:
        pass
    else:
        raise AssertionError("exhausted persisted attempt budget must fail")
    exhausted = store.read("owner-1", "op-budget")
    check(
        "attempt/time/token budgets survive restart and exhaust to attention",
        exhausted.attempt == 2
        and exhausted.tokens_used == 90
        and exhausted.deadline_at == 1060.0
        and exhausted.state == "attention-required"
        and exhausted.attention_reason == AttentionReason.RETRY_EXHAUSTED,
    )

    enforce_callback_deadline = getattr(
        runtime_worker, "enforce_callback_deadline", None
    )
    review_route = RuntimeRoute(
        "claude",
        "opus-5",
        "high",
        "reviewer-callback",
        FINGERPRINT,
    )
    deadline_spec = OperationSpec(
        "review-live-timeout",
        "key-review-live-timeout",
        "simple-review",
        "owner-1",
        review_route,
        "packet.json",
        "scoped",
    )
    store.create(
        deadline_spec,
        lane_id="lane-review-timeout",
        run_id="run-review-timeout",
    )
    deadline_supervisor = OperationSupervisor(
        store, "owner-1", "review-live-timeout"
    )
    deadline_supervisor.configure_budget(
        attempt_limit=1,
        model_restart_limit=0,
        time_budget_seconds=60,
        token_limit=100,
        now=1000.0,
    )
    for state in ("preflight", "starting", "running", "awaiting-callback"):
        store.transition("owner-1", "review-live-timeout", state)
    expired = (
        enforce_callback_deadline(
            store,
            "owner-1",
            "review-live-timeout",
            callback_handled=False,
            now=1060.0,
        )
        if enforce_callback_deadline is not None
        else False
    )
    timed_out = store.read("owner-1", "review-live-timeout")
    check(
        "live reviewer deadline becomes durable callback-timeout attention",
        expired
        and timed_out.state == "attention-required"
        and timed_out.attention_reason == AttentionReason.CALLBACK_TIMEOUT,
    )

    executor_spec = OperationSpec(
        "executor-live-timeout",
        "key-executor-live-timeout",
        "dispatch",
        "owner-1",
        route,
        "packet.json",
        "scoped",
    )
    store.create(
        executor_spec,
        lane_id="lane-executor-timeout",
        run_id="run-executor-timeout",
    )
    executor_supervisor = OperationSupervisor(
        store, "owner-1", "executor-live-timeout"
    )
    executor_supervisor.configure_budget(
        attempt_limit=1,
        model_restart_limit=0,
        time_budget_seconds=60,
        token_limit=100,
        now=1000.0,
    )
    for state in ("preflight", "starting", "running", "awaiting-callback"):
        store.transition("owner-1", "executor-live-timeout", state)
    executor_expired = enforce_callback_deadline(
        store,
        "owner-1",
        "executor-live-timeout",
        callback_handled=False,
        now=1060.0,
    )
    check(
        "live timeout stays scoped to reviewer sessions",
        not executor_expired
        and store.read("owner-1", "executor-live-timeout").state
        == "awaiting-callback",
    )
